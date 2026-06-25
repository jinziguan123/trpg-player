from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.event import ChatRequest
from app.services.chat_service import handle_chat

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def chat(
    session_id: str, data: ChatRequest, db: Session = Depends(get_db)
):
    return StreamingResponse(
        handle_chat(db, session_id, data.content),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
