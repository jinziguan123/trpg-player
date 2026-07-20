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


class AdvanceRequest(BaseModel):
    """玩家点『推进本回合』确认——所有真人都确认后才整批交 KP。"""

    acting_character_id: str | None = None


class EventEditRequest(BaseModel):
    """改写自己本回合暂存发言的正文。"""

    content: str
    acting_character_id: str | None = None


class TravelRequest(BaseModel):
    """玩家经大地图『前往』某已知地点（显式移动，确定性切换该角色所在场景）。

    stash=True：把「前往」作为本回合暂存动作加入（与发言同批，等推进时随回合一起执行位置同步 +
    叙述抵达），而非立即单独触发一次生成——这样表达「想去某处」不必再手动点图额外走一次生成。
    """

    scene_id: str
    acting_character_id: str | None = None
    stash: bool = False


class InventoryUseRequest(BaseModel):
    """玩家主动使用一件库存物品（消耗品自动 -1，效果由 KP 据本回合暂存动作叙述）。"""

    item_id: str
    acting_character_id: str | None = None


class InventoryDropRequest(BaseModel):
    """玩家丢弃/销毁一件库存物品（qty 缺省=整条移除）。"""

    item_id: str
    qty: int | None = None
    acting_character_id: str | None = None


class InventoryGiveRequest(BaseModel):
    """把一件库存物品转让给同队的另一角色。"""

    item_id: str
    to_character_id: str
    qty: int = 1
    acting_character_id: str | None = None


class StreamChunk(BaseModel):
    type: Literal[
        "narration", "dialogue", "action", "dice", "system",
        "check_request", "thinking", "done", "kp_request", "kp_action",
        "kp_turn_ready", "kp_roll_ready",
    ]
    actor_name: str | None = None
    content: str = ""
    metadata: dict = {}
