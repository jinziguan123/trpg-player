from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SessionCreate(BaseModel):
    module_id: str
    player_character_id: str


class SessionRead(BaseModel):
    id: str
    module_id: str
    status: str
    player_character_id: str | None
    current_scene_id: str | None
    world_state: dict
    turn_state: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionStatusUpdate(BaseModel):
    status: str
