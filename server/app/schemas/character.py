from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CharacterCreate(BaseModel):
    name: str
    module_id: str
    rule_system: str
    is_player: bool = True
    base_attributes: dict[str, int] = {}
    skills: dict[str, int] = {}
    system_data: dict = {}
    backstory: str = ""


class CharacterRead(BaseModel):
    id: str
    name: str
    module_id: str | None
    rule_system: str
    is_player: bool
    base_attributes: dict
    skills: dict
    system_data: dict
    backstory: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CharacterUpdate(BaseModel):
    name: str | None = None
    base_attributes: dict | None = None
    skills: dict | None = None
    system_data: dict | None = None
    backstory: str | None = None
    status: str | None = None


class RollAttributesResponse(BaseModel):
    sets: list[dict[str, int]]
