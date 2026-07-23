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
    map_nodes: list = []
    triggers: list = []
    # 手书（信件/报纸/日记/便条等原文文书），跑团中经 [HANDOUT] 指令发放
    handouts: list = []
    # 幕后真相（守秘人资讯）：KP 专属参考，详情页带剧透警告展示
    truth: str = ""
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
    # None 表示旧客户端未提交地图节点，更新时保留服务端已有普通节点。
    map_nodes: list | None = None
    npcs: list = []
    clues: list = []
    triggers: list = []
    handouts: list = []
    # None = 本次编辑不动 truth（update_module 跳过 None，防旧前端把真相清空）
    truth: str | None = None


class ModuleUploadResponse(BaseModel):
    id: str
    title: str
    rule_system: str
    description: str
    scenes_count: int
    npcs_count: int
    clues_count: int
