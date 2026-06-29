import json
import logging

from sqlalchemy.orm import Session

from app.ai.llm_factory import get_llm
from app.models.module import Module

logger = logging.getLogger(__name__)

PARSE_PROMPT_TEMPLATE = """你是一个 {rule_system} 模组分析专家。
请仔细阅读以下模组文本，提取结构化信息并以 JSON 格式返回。

要求的 JSON 结构：
{{
  "title": "模组标题",
  "description": "一句话简介（不超过30字，不要透露关键剧情）",
  "player_brief": "开场时玩家角色就合法知道的背景：他们的身份动机、当前处境、接到的委托或为何来到起始地点。只写玩家此刻本就清楚的前情，绝对不要包含任何需要在游戏中被发现的内容（尸体、笔记、隐藏线索、NPC 的秘密、剧情真相、失踪者下落等）。若模组没有明确的玩家前情，留空字符串。",
  "player_count": "推荐游玩人数，如 1-4",
  "era": "背景年代标签，如 1920s、现代、中世纪、维多利亚时代",
  "difficulty": "难度等级，仅限以下四选一：入门/普通/困难/噩梦",
  "tags": ["模组主题标签，如 恐怖、悬疑、冒险、密室、调查、战斗 等，2-4个"],
  "world_setting": {{
    "era": "详细时代背景描述",
    "location": "地点",
    "tone": "基调（如恐怖、悬疑、冒险）"
  }},
  "scenes": [
    {{
      "id": "scene_1",
      "title": "场景标题",
      "description": "场景详细描述",
      "danger": "该场景的危险等级，仅限四选一：calm（安全平静）/uneasy（隐隐不安）/dangerous（明确危险）/deadly（致命凶险）",
      "atmosphere": "一句话氛围基调，给 KP 渲染用：以感官（声/味/光/体感）+ 情绪基调描述，如『腐臭、低压、木板随时塌陷』。不要写成剧透或台词",
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
      "initial_location": "scene_1",
      "skills": {{"战斗": 55, "闪避": 40, "侦查": 60, "潜行": 50, "心理学": 45}}
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
5. description 必须简短，绝对不要包含剧情细节
6. player_brief 与 secrets/clues 严格分离：凡是玩家要靠调查/检定才能知道的，一律放进 secrets/clues，绝不写进 player_brief
7. 每个 NPC 给出 skills：与其身份相符的关键技能数值（0-90 整数）。优先采用模组原文给的数值；
   原文没有就按角色定位合理估计（如守卫战斗高、学者知识高、普通人多在 40-50）。至少覆盖可能用到的
   对抗/侦查/social 类技能（战斗、闪避、侦查、潜行、聆听、话术、心理学等），供 KP 暗骰与对抗骰使用
8. difficulty 根据模组战斗频率、解谜难度、角色死亡风险综合判断
9. 每个场景给出 danger（四选一枚举）与 atmosphere（一句话氛围）：danger 按该场景的实际威胁程度判定，
   多数调查/日常场景是 calm 或 uneasy，只有真正有战斗/陷阱/神话冲击的场景才 dangerous/deadly；
   atmosphere 只写基调与感官，绝不能泄露需要被发现的线索或真相

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
    world_setting = data.get("world_setting", {})
    for key in ("player_count", "era", "difficulty", "tags", "player_brief"):
        if key in data:
            world_setting[key] = data[key]

    module = Module(
        title=data.get("title", "未命名模组"),
        rule_system=data.get("rule_system", "coc"),
        description=data.get("description", ""),
        world_setting=world_setting,
        raw_content=raw_content,
        scenes=data.get("scenes", []),
        npcs=data.get("npcs", []),
        clues=data.get("clues", []),
    )
    db.add(module)
    db.commit()
    db.refresh(module)
    return module


def update_module(db: Session, module_id: str, data: dict) -> Module | None:
    """整体更新模组的结构化内容（手动编辑）。world_setting/scenes/npcs/clues 直接替换。"""
    module = db.get(Module, module_id)
    if not module:
        return None
    if "title" in data:
        module.title = data["title"] or module.title
    if "rule_system" in data and data["rule_system"]:
        module.rule_system = data["rule_system"]
    if "description" in data:
        module.description = data["description"]
    if "world_setting" in data and data["world_setting"] is not None:
        module.world_setting = data["world_setting"]
    if "scenes" in data and data["scenes"] is not None:
        module.scenes = data["scenes"]
    if "npcs" in data and data["npcs"] is not None:
        module.npcs = data["npcs"]
    if "clues" in data and data["clues"] is not None:
        module.clues = data["clues"]
    db.commit()
    db.refresh(module)
    return module


def get_module(db: Session, module_id: str) -> Module | None:
    return db.get(Module, module_id)


def list_modules(db: Session) -> list[Module]:
    return db.query(Module).order_by(Module.created_at.desc()).all()


def delete_module(db: Session, module_id: str) -> bool:
    module = db.get(Module, module_id)
    if not module:
        return False
    db.delete(module)
    db.commit()
    return True
