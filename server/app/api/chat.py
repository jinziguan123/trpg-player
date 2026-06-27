from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.models.session import GameSession
from app.schemas.event import ChatRequest
from app.services import session_service
from app.services.chat_service import (
    _make_chunk,
    event_to_chunk,
    run_chat_generation,
    split_ooc,
)
from app.services.generation_manager import generation_manager
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/ooc")
def post_ooc(
    session_id: str,
    data: ChatRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """纯 OOC（场外）消息：入库并向全房间广播，不进入 KP 上下文、不触发任何生成。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    try:
        actor = session_service.resolve_actor(
            db, session_id, token, data.acting_character_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))

    _, ooc = split_ooc(data.content)
    text = ooc or data.content.strip()
    ev = session_service.add_event(
        db, session_id, "ooc", text,
        actor_id=actor.id, actor_name=actor.name,
    )
    room_hub.broadcast(session_id, event_to_chunk(ev))
    return {"ok": True, "id": ev.id}


@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    data: ChatRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """fire-and-forget：校验 + 落库玩家行动并广播 + 触发生成；输出统一经 /live 下发。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")

    try:
        player_char = session_service.resolve_actor(
            db, session_id, token, data.acting_character_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))

    in_character, ooc = split_ooc(data.content)
    if not in_character:
        ev = session_service.add_event(
            db, session_id, "ooc", ooc or data.content.strip(),
            actor_id=player_char.id, actor_name=player_char.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev))
        raise HTTPException(400, "该消息为纯场外发言，请使用 OOC 通道")

    # 正式行动：括号外交给 KP（广播给全房间）；括号内作为独立 OOC（不入 KP 上下文）
    ev = session_service.add_event(
        db, session_id, "dialogue", in_character,
        actor_id=player_char.id, actor_name=player_char.name,
    )
    room_hub.broadcast(session_id, event_to_chunk(ev))
    if ooc:
        ev_ooc = session_service.add_event(
            db, session_id, "ooc", ooc,
            actor_id=player_char.id, actor_name=player_char.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev_ooc))

    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(session_id, run_chat_generation(session_id))
    return {"ok": True}
