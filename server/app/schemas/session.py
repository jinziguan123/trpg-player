from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator


class ParticipantInput(BaseModel):
    """开局时前端提交的一个席位。

    character_id 为空且 role=human 表示「留空待加入」的空席（claimed=false）。
    """

    character_id: str | None = None
    role: str = "ai"  # human | ai
    is_primary: bool = False


class ParticipantRead(BaseModel):
    character_id: str | None = None
    role: str
    is_primary: bool
    seat_order: int
    claimed: bool = True
    ready: bool = True
    character_name: str | None = None
    is_mine: bool = False  # 该席位是否归当前请求 token 所有（由端点按 token 计算）
    is_host: bool = False  # 该席位是否房主（主角席 + 有 owner_token），端点按 token 计算

    model_config = {"from_attributes": True}


class SessionCreate(BaseModel):
    module_id: str
    # 旧单人路径：只传主角；新多席位路径：传 participants（含主角 + AI 队友 + 空席）
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
    room_code: str | None = None
    current_scene_id: str | None
    world_state: dict
    turn_state: dict | None
    participants: list[ParticipantRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionStatusUpdate(BaseModel):
    status: str


class ClaimSeatRequest(BaseModel):
    seat_order: int
    character_id: str


class ReadyRequest(BaseModel):
    ready: bool = True
