from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import player_token, require_session_kp
from app.database import get_db
from app.models.module import Module
from app.schemas.kp import (
    KpDraftRequest,
    KpImagePreviewRequest,
    KpImagePublishRequest,
    KpPlanRequest,
    KpWorkspaceUpdate,
)
from app.services import human_kp_service, session_service
from app.services.chat_service import _make_chunk, event_to_chunk
from app.services.generation_manager import generation_manager
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["human-kp"])


def _kp_context(
    db: Session, session_id: str, token: str | None,
):
    game_session = require_session_kp(db, session_id, token)
    module = db.get(Module, game_session.module_id)
    if module is None:
        raise HTTPException(404, "模组不存在")
    return game_session, module


def _advisor_error(error: Exception) -> HTTPException:
    if isinstance(error, ValueError):
        return HTTPException(400, str(error))
    return HTTPException(502, "AI 参谋暂时不可用，请检查模型配置后重试")


@router.get("/{session_id}/kp/workspace")
def get_workspace(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, module = _kp_context(db, session_id, token)
    return human_kp_service.workspace_payload(db, session_id, game_session, module)


@router.patch("/{session_id}/kp/workspace")
def update_workspace(
    session_id: str,
    data: KpWorkspaceUpdate,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, module = _kp_context(db, session_id, token)
    human_kp_service.update_workspace(
        db, game_session,
        notes=data.notes,
        auto_ai_teammates=data.auto_ai_teammates,
    )
    return human_kp_service.workspace_payload(db, session_id, game_session, module)


@router.post("/{session_id}/kp/advisor/draft")
async def generate_draft(
    session_id: str,
    data: KpDraftRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, module = _kp_context(db, session_id, token)
    try:
        draft = await human_kp_service.generate_narration_draft(
            db, session_id, game_session, module, data.instruction,
        )
    except Exception as error:  # noqa: BLE001
        raise _advisor_error(error) from error
    return {"draft": draft}


@router.post("/{session_id}/kp/advisor/plan")
async def generate_plan(
    session_id: str,
    data: KpPlanRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, module = _kp_context(db, session_id, token)
    try:
        plan = await human_kp_service.generate_turn_plan(
            db, session_id, game_session, module, data.focus,
        )
    except Exception as error:  # noqa: BLE001
        raise _advisor_error(error) from error
    return {"plan": plan}


@router.get("/{session_id}/kp/lookup")
def lookup(
    session_id: str,
    scope: Literal["rule", "module"] = Query(),
    q: str = Query(min_length=1, max_length=500),
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, module = _kp_context(db, session_id, token)
    try:
        hits = human_kp_service.lookup(db, game_session, module, scope, q)
    except Exception as error:  # noqa: BLE001
        raise _advisor_error(error) from error
    return {"hits": hits}


@router.post("/{session_id}/kp/images/preview")
async def preview_image(
    session_id: str,
    data: KpImagePreviewRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    _kp_context(db, session_id, token)
    try:
        return await human_kp_service.generate_image_preview(data.prompt, data.title)
    except Exception as error:  # noqa: BLE001
        raise _advisor_error(error) from error


@router.post("/{session_id}/kp/images/publish")
def publish_image(
    session_id: str,
    data: KpImagePublishRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    _kp_context(db, session_id, token)
    try:
        event = human_kp_service.publish_image(
            db, session_id, data.url, data.title, data.suggestion_key,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    room_hub.broadcast(session_id, event_to_chunk(event))
    return {"ok": True, "event_id": event.id}


@router.post("/{session_id}/kp/team-turn")
async def run_team_turn(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    game_session, _module = _kp_context(db, session_id, token)
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "当前仍有动作处理中，请稍候")
    if not session_service.get_ai_teammates(db, session_id):
        raise HTTPException(400, "本局没有 AI 队友")
    if not human_kp_service.has_unprocessed_player_turn(db, session_id, game_session):
        raise HTTPException(409, "当前没有尚未处理的新玩家回合")
    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(
        session_id, human_kp_service.run_human_team_turn_generation(session_id),
    )
    return {"ok": True}


@router.post("/{session_id}/kp/end-turn")
def end_kp_turn(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    _kp_context(db, session_id, token)
    room_hub.broadcast(
        session_id,
        _make_chunk(
            "turn_state",
            metadata={"confirmed_ids": [], "total": 0, "ready": False},
        ),
    )
    room_hub.broadcast(session_id, _make_chunk("done"))
    return {"ok": True}
