from pydantic import BaseModel


class OnboardingStartResponse(BaseModel):
    session_id: str
    status: str
    reused: bool
