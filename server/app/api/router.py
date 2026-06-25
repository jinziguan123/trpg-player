from fastapi import APIRouter

from app.api.characters import router as characters_router
from app.api.chat import router as chat_router
from app.api.modules import router as modules_router
from app.api.sessions import router as sessions_router

api_router = APIRouter()
api_router.include_router(modules_router)
api_router.include_router(characters_router)
api_router.include_router(sessions_router)
api_router.include_router(chat_router)
