from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import ai_settings
from app.api.deps import player_token
from app.database import get_db
from app.schemas.onboarding import OnboardingStartResponse
from app.services.onboarding_service import start_onboarding

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.post("/start", response_model=OnboardingStartResponse)
def start(
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
) -> OnboardingStartResponse:
    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="缺少玩家身份")

    profile = ai_settings.load_active_profile()
    if not profile or not profile.api_key.strip() or not profile.model_name.strip():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ai_not_configured",
                "message": "请先配置并测试可用的 AI 模型",
            },
        )

    game, reused = start_onboarding(db, token.strip())
    return OnboardingStartResponse(
        session_id=game.id,
        status=game.status,
        reused=reused,
    )
