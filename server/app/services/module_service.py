import json
import logging

from sqlalchemy.orm import Session

from app.ai.deepseek import get_llm
from app.models.module import Module

logger = logging.getLogger(__name__)

PARSE_PROMPT_TEMPLATE = """你是一个 {rule_system} 模组分析专家。
请仔细阅读以下模组文本，提取结构化信息并以 JSON 格式返回。

要求的 JSON 结构：
{{
  "title": "模组标题",
  "description": "模组简介（2-3句话）",
  "world_setting": {{
    "era": "时代背景",
    "location": "地点",
    "tone": "基调（如恐怖、悬疑、冒险）"
  }},
  "scenes": [
    {{
      "id": "scene_1",
      "title": "场景标题",
      "description": "场景详细描述",
      "connections": ["scene_2"]
    }}
  ],
  "npcs": [
    {{
      "id": "npc_1",
      "name": "NPC名字",
      "description": "外貌和身份描述",
      "personality": "性格特点和行为方式",
      "secrets": ["只有KP知道的秘密"],
      "initial_location": "scene_1"
    }}
  ],
  "clues": [
    {{
      "id": "clue_1",
      "name": "线索名称",
      "description": "线索内容",
      "location": "scene_1",
      "trigger_condition": "如何发现这个线索"
    }}
  ]
}}

请确保：
1. 每个场景、NPC、线索都有唯一的 id
2. NPC 的 secrets 是玩家不应该直接知道的信息
3. 线索的 trigger_condition 描述玩家需要做什么才能发现
4. 场景的 connections 标明可以从该场景前往的其他场景

模组文本：
{content}"""


async def parse_module_text(raw_text: str, rule_system: str) -> dict:
    """用 AI 解析模组文本为结构化数据"""
    llm = get_llm()
    prompt = PARSE_PROMPT_TEMPLATE.format(
        rule_system=rule_system.upper(), content=raw_text
    )

    result = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=8192,
    )

    return json.loads(result)


def create_module(db: Session, data: dict, raw_content: str = "") -> Module:
    module = Module(
        title=data.get("title", "未命名模组"),
        rule_system=data.get("rule_system", "coc"),
        description=data.get("description", ""),
        world_setting=data.get("world_setting", {}),
        raw_content=raw_content,
        scenes=data.get("scenes", []),
        npcs=data.get("npcs", []),
        clues=data.get("clues", []),
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    return module


def get_module(db: Session, module_id: str) -> Module | None:
    return db.get(Module, module_id)


def list_modules(db: Session) -> list[Module]:
    return db.query(Module).order_by(Module.created_at.desc()).all()
