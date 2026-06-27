from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.character import Character
from app.models.session import GameSession
from app.schemas.event import ChatRequest
from app.services import session_service
from app.services.chat_service import run_chat_generation
from app.services.generation_manager import generation_manager, stream_from_queue

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def chat(
    session_id: str, data: ChatRequest, db: Session = Depends(get_db),
):
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "正在生成中，请等待")

    player_char = db.get(Character, game_session.player_character_id)
    if not player_char:
        raise HTTPException(400, "角色数据缺失")

    session_service.add_event(
        db, session_id, "dialogue", data.content,
        actor_id=player_char.id, actor_name=player_char.name,
    )

    q = generation_manager.start(session_id, run_chat_generation(session_id))

    return StreamingResponse(
        stream_from_queue(session_id, q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
