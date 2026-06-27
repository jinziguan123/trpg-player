from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.models.character import Character
from app.models.module import Module
from app.schemas.event import EventRead
from app.schemas.session import (
    ClaimSeatRequest,
    ReadyRequest,
    SessionCreate,
    SessionRead,
    SessionStatusUpdate,
)
from app.services import session_service
from app.services.chat_service import _make_chunk, event_to_chunk, run_opening_generation
from app.services.generation_manager import generation_manager
from app.services.room_hub import room_hub, stream_room

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _session_payload(
    session, chars_map: dict[str, str], module_title: str | None,
    token: str | None = None,
) -> dict:
    data = SessionRead.model_validate(session).model_dump()
    # session.participants 与 data["participants"] 均按 seat_order，可对齐计算 is_mine
    for p, sp in zip(data.get("participants", []), session.participants):
        p["character_name"] = chars_map.get(p["character_id"]) if p["character_id"] else None
        p["is_mine"] = bool(token and sp.owner_token and sp.owner_token == token)
        p["is_host"] = bool(sp.is_primary and sp.owner_token)
    return {
        **data,
        "module_title": module_title,
        "character_name": (
            chars_map.get(session.player_character_id)
            if session.player_character_id
            else None
        ),
    }


def _chars_map(db: Session, sessions) -> dict[str, str]:
    char_ids: set[str] = set()
    for s in sessions:
        if s.player_character_id:
            char_ids.add(s.player_character_id)
        char_ids.update(p.character_id for p in s.participants if p.character_id)
    if not char_ids:
        return {}
    return {
        c.id: c.name
        for c in db.query(Character).filter(Character.id.in_(char_ids)).all()
    }


@router.post("")
def create_session(
    data: SessionCreate,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    if data.participants:
        seats = [p.model_dump() for p in data.participants]
    else:
        seats = [
            {"character_id": data.player_character_id, "role": "human", "is_primary": True}
        ]
    try:
        session = session_service.create_session(db, data.module_id, seats, creator_token=token)
    except ValueError as e:
        raise HTTPException(400, str(e))

    module = db.get(Module, session.module_id)
    return _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )


@router.get("")
def list_sessions(
    db: Session = Depends(get_db), token: str | None = Depends(player_token),
):
    sessions = session_service.list_sessions(db)
    module_ids = {s.module_id for s in sessions}
    modules_map = (
        {m.id: m.title for m in db.query(Module).filter(Module.id.in_(module_ids)).all()}
        if module_ids else {}
    )
    chars_map = _chars_map(db, sessions)
    return [
        _session_payload(s, chars_map, modules_map.get(s.module_id), token)
        for s in sessions
    ]


@router.get("/by-code/{room_code}")
def get_session_by_code(
    room_code: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    session = session_service.get_session_by_code(db, room_code)
    if not session:
        raise HTTPException(404, "房间不存在或房间码有误")
    module = db.get(Module, session.module_id)
    return _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )


@router.post("/{session_id}/claim")
def claim_seat(
    session_id: str,
    data: ClaimSeatRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    try:
        session = session_service.claim_seat(
            db, session_id, data.seat_order, data.character_id, token or "",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    module = db.get(Module, session.module_id)
    payload = _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )
    # 广播入座事件给房间内所有人
    seat = next((p for p in payload["participants"] if p["seat_order"] == data.seat_order), None)
    name = seat["character_name"] if seat else "新成员"
    room_hub.broadcast(session_id, _make_chunk("seat", f"{name} 已入座", actor_name=name))
    return payload


@router.post("/{session_id}/ready")
def set_ready(
    session_id: str,
    data: ReadyRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """大厅：把当前玩家席位的准备态置位，并广播 lobby 刷新。"""
    try:
        session = session_service.set_ready(db, session_id, token, data.ready)
    except ValueError as e:
        raise HTTPException(400, str(e))
    module = db.get(Module, session.module_id)
    payload = _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )
    room_hub.broadcast(session_id, _make_chunk("lobby"))
    return payload


@router.post("/{session_id}/start")
def start_game(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """大厅：房主开局。校验门槛后 setup→active，并触发开场生成。"""
    try:
        session = session_service.start_game(db, session_id, token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 推进到 active 后触发开场（fire-and-forget，输出经 /live 下发）
    room_hub.broadcast(session_id, _make_chunk("started"))
    if not generation_manager.is_generating(session_id):
        room_hub.broadcast(session_id, _make_chunk("generating"))
        generation_manager.start(session_id, run_opening_generation(session_id))
    module = db.get(Module, session.module_id)
    return _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )


@router.get("/{session_id}")
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    module = db.get(Module, session.module_id)
    return _session_payload(
        session, _chars_map(db, [session]), module.title if module else None, token,
    )


@router.put("/{session_id}/status", response_model=SessionRead)
def update_status(
    session_id: str, data: SessionStatusUpdate, db: Session = Depends(get_db)
):
    session = session_service.update_session_status(db, session_id, data.status)
    if not session:
        raise HTTPException(404, "会话不存在")
    return session


@router.get("/{session_id}/events")
def get_events(
    session_id: str,
    limit: int = 50,
    before_seq: int | None = None,
    db: Session = Depends(get_db),
):
    events, has_more = session_service.get_latest_events(
        db, session_id, limit=limit, before_seq=before_seq,
    )
    return {
        "events": [EventRead.model_validate(e).model_dump() for e in events],
        "has_more": has_more,
    }


@router.delete("/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    if not session_service.delete_session(db, session_id):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@router.post("/{session_id}/opening")
async def trigger_opening(session_id: str, db: Session = Depends(get_db)):
    """fire-and-forget 触发开场生成；输出经 /live 下发。幂等由生成逻辑保证。"""
    game_session = session_service.get_session(db, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if generation_manager.is_generating(session_id):
        return {"ok": True, "already_generating": True}

    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(session_id, run_opening_generation(session_id))
    return {"ok": True}


@router.get("/{session_id}/generating")
async def check_generating(session_id: str):
    return {"generating": generation_manager.is_generating(session_id)}


@router.get("/{session_id}/live")
async def live(session_id: str):
    """房间级常驻 SSE（仅实时增量）：所有成员订阅，跨多次生成存活。

    历史与重连对齐沿用 ``GET /events``（保留 seq 分页）；本端点只负责实时广播：
    玩家行动、KP 叙事 token、检定、OOC、入座/在场等。客户端先开本连接、再拉历史，
    按事件 id 去重，避免开连接与拉历史之间的竞态丢事件。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if not session_service.get_session(db, session_id):
            raise HTTPException(404, "会话不存在")
    finally:
        db.close()

    # subscribe 会把当前生成的 in-flight buffer 立即重放给中途接入者
    q = room_hub.subscribe(session_id)
    generating = generation_manager.is_generating(session_id)

    async def gen():
        yield _make_chunk("ready")
        if generating:
            yield _make_chunk("generating")
        async for chunk in stream_room(session_id, q):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
