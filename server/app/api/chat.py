from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.character import Character
from app.models.session import GameSession
from app.schemas.event import ChatRequest
from app.services import session_service
from app.services.chat_service import run_chat_generation, split_ooc
from app.services.generation_manager import generation_manager, stream_from_queue

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/ooc")
def post_ooc(session_id: str, data: ChatRequest, db: Session = Depends(get_db)):
    """纯 OOC（场外）消息：只入库 / 广播，不进入 KP 上下文、不触发任何生成。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    player_char = db.get(Character, game_session.player_character_id)
    if not player_char:
        raise HTTPException(400, "角色数据缺失")

    _, ooc = split_ooc(data.content)
    text = ooc or data.content.strip()
    ev = session_service.add_event(
        db, session_id, "ooc", text,
        actor_id=player_char.id, actor_name=player_char.name,
    )
    return {"ok": True, "id": ev.id}


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

    in_character, ooc = split_ooc(data.content)
    if not in_character:
        # 纯 OOC：不应走到这里（前端应改调 /ooc），兜底当 OOC 处理，不触发生成
        session_service.add_event(
            db, session_id, "ooc", ooc or data.content.strip(),
            actor_id=player_char.id, actor_name=player_char.name,
        )
        raise HTTPException(400, "该消息为纯场外发言，请使用 OOC 通道")

    # 正式行动只把括号外内容交给 KP；括号内作为独立 OOC 记录（不入 KP 上下文）
    session_service.add_event(
        db, session_id, "dialogue", in_character,
        actor_id=player_char.id, actor_name=player_char.name,
    )
    if ooc:
        session_service.add_event(
            db, session_id, "ooc", ooc,
            actor_id=player_char.id, actor_name=player_char.name,
        )

    q = generation_manager.start(session_id, run_chat_generation(session_id))

    return StreamingResponse(
        stream_from_queue(session_id, q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
