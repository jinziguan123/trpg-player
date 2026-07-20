from fastapi import APIRouter

from app.api.ai_settings import router as ai_settings_router
from app.api.characters import router as characters_router
from app.api.chat import router as chat_router
from app.api.combat import router as combat_router
from app.api.inventory import router as inventory_router
from app.api.kp import router as kp_router
from app.api.images import router as images_router
from app.api.modules import router as modules_router
from app.api.onboarding import router as onboarding_router
from app.api.rulebooks import router as rulebooks_router
from app.api.sessions import router as sessions_router

api_router = APIRouter()
api_router.include_router(images_router)
api_router.include_router(modules_router)
api_router.include_router(onboarding_router)
api_router.include_router(characters_router)
api_router.include_router(sessions_router)
api_router.include_router(chat_router)
api_router.include_router(kp_router)
api_router.include_router(combat_router)
api_router.include_router(inventory_router)
api_router.include_router(ai_settings_router)
api_router.include_router(rulebooks_router)
