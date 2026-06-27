from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.character import Character
from app.models.module import Module
from app.schemas.event import EventRead
from app.schemas.session import SessionCreate, SessionRead, SessionStatusUpdate
from app.services import session_service
from app.services.chat_service import run_opening_generation
from app.services.generation_manager import generation_manager, stream_from_queue

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionRead)
async def create_session(data: SessionCreate, db: Session = Depends(get_db)):
    try:
        session = session_service.create_session(
            db, data.module_id, data.player_character_id
        )
        return session
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("")
def list_sessions(db: Session = Depends(get_db)):
    sessions = session_service.list_sessions(db)
    module_ids = {s.module_id for s in sessions}
    char_ids = {s.player_character_id for s in sessions if s.player_character_id}

    modules_map = (
        {m.id: m.title for m in db.query(Module).filter(Module.id.in_(module_ids)).all()}
        if module_ids else {}
    )
    chars_map = (
        {c.id: c.name for c in db.query(Character).filter(Character.id.in_(char_ids)).all()}
        if char_ids else {}
    )

    return [
        {
            **SessionRead.model_validate(s).model_dump(),
            "module_title": modules_map.get(s.module_id),
            "character_name": chars_map.get(s.player_character_id) if s.player_character_id else None,
        }
        for s in sessions
    ]


@router.get("/{session_id}", response_model=SessionRead)
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    return session


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
    game_session = session_service.get_session(db, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "正在生成中")

    q = generation_manager.start(session_id, run_opening_generation(session_id))

    return StreamingResponse(
        stream_from_queue(session_id, q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/generating")
async def check_generating(session_id: str):
    return {"generating": generation_manager.is_generating(session_id)}


@router.get("/{session_id}/stream")
async def subscribe_stream(session_id: str):
    if not generation_manager.is_generating(session_id):
        return Response(status_code=204)

    q = generation_manager.subscribe(session_id)

    return StreamingResponse(
        stream_from_queue(session_id, q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
