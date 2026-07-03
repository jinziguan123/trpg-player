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
    triggers: list = []
    # 手书（信件/报纸/日记/便条等原文文书），跑团中经 [HANDOUT] 指令发放
    handouts: list = []
    # 原文 RAG 索引状态：""=未建 / indexing / ready / failed
    rag_status: str = ""
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModuleWrite(BaseModel):
    """手动新建/编辑模组的结构化内容。"""

    title: str
    rule_system: str = "coc"
    description: str = ""
    world_setting: dict = {}
    scenes: list = []
    npcs: list = []
    clues: list = []
    triggers: list = []
    handouts: list = []


class ModuleUploadResponse(BaseModel):
    id: str
    title: str
    rule_system: str
    description: str
    scenes_count: int
    npcs_count: int
    clues_count: int
