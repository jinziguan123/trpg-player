from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ModuleCreate(BaseModel):
    title: str
    rule_system: str
    description: str = ""


class ModuleRead(BaseModel):
    id: str
    title: str
    rule_system: str
    description: str
    theme: str
    world_setting: dict
    scenes: list
    npcs: list
    clues: list
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModuleUploadResponse(BaseModel):
    id: str
    title: str
    rule_system: str
    description: str
    scenes_count: int
    npcs_count: int
    clues_count: int
