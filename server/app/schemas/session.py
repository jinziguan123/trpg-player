from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator


class ParticipantInput(BaseModel):
    """开局时前端提交的一个席位。"""

    character_id: str
    role: str = "ai"  # human | ai
    is_primary: bool = False


class ParticipantRead(BaseModel):
    character_id: str
    role: str
    is_primary: bool
    seat_order: int
    character_name: str | None = None

    model_config = {"from_attributes": True}


class SessionCreate(BaseModel):
    module_id: str
    # 旧单人路径：只传主角；新多席位路径：传 participants（含主角 + AI 队友）。
    player_character_id: str | None = None
    participants: list[ParticipantInput] | None = None

    @model_validator(mode="after")
    def _require_seat(self) -> "SessionCreate":
        if not self.participants and not self.player_character_id:
            raise ValueError("必须至少提供一个主角席位")
        return self


class SessionRead(BaseModel):
    id: str
    module_id: str
    status: str
    player_character_id: str | None
    current_scene_id: str | None
    world_state: dict
    turn_state: dict | None
    participants: list[ParticipantRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionStatusUpdate(BaseModel):
    status: str
