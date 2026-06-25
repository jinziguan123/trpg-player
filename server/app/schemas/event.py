from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class EventRead(BaseModel):
    id: str
    session_id: str
    sequence_num: int
    event_type: str
    actor_id: str | None
    actor_name: str
    content: str
    visibility: list[str]
    metadata_: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    content: str


class StreamChunk(BaseModel):
    type: Literal[
        "narration", "dialogue", "action", "dice", "system", "thinking", "done"
    ]
    actor_name: str | None = None
    content: str = ""
    metadata: dict = {}
