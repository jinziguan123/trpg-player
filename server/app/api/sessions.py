from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.character import Character
from app.models.module import Module
from app.schemas.event import EventRead
from app.schemas.session import SessionCreate, SessionRead, SessionStatusUpdate
from app.services import session_service
from app.services.chat_service import _make_chunk, run_opening_generation
from app.services.generation_manager import generation_manager
from app.services.room_hub import room_hub, stream_room

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _session_payload(
    session, chars_map: dict[str, str], module_title: str | None
) -> dict:
    data = SessionRead.model_validate(session).model_dump()
    for p in data.get("participants", []):
        p["character_name"] = chars_map.get(p["character_id"])
    return {
        **data,
        "module_title": module_title,
        "character_name": (
            chars_map.get(session.player_character_id)
            if session.player_character_id
            else None
        ),
    }


@router.post("")
def create_session(data: SessionCreate, db: Session = Depends(get_db)):
    if data.participants:
        seats = [p.model_dump() for p in data.participants]
    else:
        # 旧单人路径：只有主角
        seats = [
            {
                "character_id": data.player_character_id,
                "role": "human",
                "is_primary": True,
            }
        ]
    try:
        session = session_service.create_session(db, data.module_id, seats)
    except ValueError as e:
        raise HTTPException(400, str(e))

    char_ids = {p.character_id for p in session.participants}
    chars_map = {
        c.id: c.name
        for c in db.query(Character).filter(Character.id.in_(char_ids)).all()
    } if char_ids else {}
    module = db.get(Module, session.module_id)
    return _session_payload(session, chars_map, module.title if module else None)


@router.get("")
def list_sessions(db: Session = Depends(get_db)):
    sessions = session_service.list_sessions(db)
    module_ids = {s.module_id for s in sessions}
    char_ids: set[str] = set()
    for s in sessions:
        if s.player_character_id:
            char_ids.add(s.player_character_id)
        char_ids.update(p.character_id for p in s.participants)

    modules_map = (
        {m.id: m.title for m in db.query(Module).filter(Module.id.in_(module_ids)).all()}
        if module_ids else {}
    )
    chars_map = (
        {c.id: c.name for c in db.query(Character).filter(Character.id.in_(char_ids)).all()}
        if char_ids else {}
    )

    return [
        _session_payload(s, chars_map, modules_map.get(s.module_id))
        for s in sessions
    ]


@router.get("/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    char_ids = {p.character_id for p in session.participants}
    if session.player_character_id:
        char_ids.add(session.player_character_id)
    chars_map = {
        c.id: c.name
        for c in db.query(Character).filter(Character.id.in_(char_ids)).all()
    } if char_ids else {}
    module = db.get(Module, session.module_id)
    return _session_payload(session, chars_map, module.title if module else None)


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
