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
    # 多人：以哪个角色发言（不传则用会话主角，兼容单人）
    acting_character_id: str | None = None


class CheckRequest(BaseModel):
    """玩家『申请』技能/属性检定——只报技能，难度由 KP 裁定（玩家不指定）。"""

    skill: str
    # 想对什么做检定的简短描述（可选）：场景里有多条线索时，光报技能名 KP 猜不出目标是哪个。
    intent: str = ""
    acting_character_id: str | None = None


class RollRequest(BaseModel):
    """玩家对一个待定检定点『投骰』。"""

    check_id: str


class TravelRequest(BaseModel):
    """玩家经大地图『前往』某已知地点（显式移动，确定性切换该角色所在场景）。"""

    scene_id: str
    acting_character_id: str | None = None


class StreamChunk(BaseModel):
    type: Literal[
        "narration", "dialogue", "action", "dice", "system",
        "check_request", "thinking", "done",
    ]
    actor_name: str | None = None
    content: str = ""
    metadata: dict = {}
