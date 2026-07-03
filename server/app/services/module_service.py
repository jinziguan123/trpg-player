import json
import logging

from sqlalchemy.orm import Session

from app.ai.llm_factory import get_llm
from app.models.module import Module

logger = logging.getLogger(__name__)

# 模组难度枚举（唯一真源；AI 解析与手动编辑都只允许这四档）
MODULE_DIFFICULTIES: tuple[str, ...] = ("入门", "普通", "困难", "噩梦")

PARSE_PROMPT_TEMPLATE = """你是一个 {rule_system} 模组分析专家。
请仔细阅读以下模组文本，提取结构化信息并以 JSON 格式返回。

要求的 JSON 结构：
{{
  "title": "模组标题",
  "description": "一句话简介（不超过30字，不要透露关键剧情）",
  "player_brief": "开场时玩家角色就合法知道的背景：他们的身份动机、当前处境、接到的委托或为何来到起始地点。只写玩家此刻本就清楚的前情，绝对不要包含任何需要在游戏中被发现的内容（尸体、笔记、隐藏线索、NPC 的秘密、剧情真相、失踪者下落等）。若模组没有明确的玩家前情，留空字符串。",
  "intro": "面向全桌的【世界观与基调导入】，开场时朗读用：年代质感、地点风物、这是一类什么样的故事（恐怖/悬疑/冒险的调性与预期、内容警示）。它和 player_brief 不同——player_brief 是角色剧内已知的前情事实，intro 是把玩家带入世界的氛围与世界观铺陈。同样严守无剧透：绝不包含任何需要在游戏中被发现的线索/真相/NPC 秘密。若模组没有值得铺陈的世界观，留空字符串。",
  "player_count": "推荐游玩人数，如 1-4",
  "era": "背景年代标签，如 1920s、现代、中世纪、维多利亚时代",
  "region": "地区标签：模组主要发生地，简短一个，如 阿卡姆、伦敦、埃及、上海、北海道、虚构小镇名等",
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
      "connections": ["scene_2"],
      "states": [
        {{"when": ["剧情标志名，如 basement_flooded"], "danger": "切换后的危险度", "atmosphere": "切换后的氛围", "description": "（可选）切换后的场景描述，覆盖默认", "structural": false}}
      ]
    }}
  ],
  "npcs": [
    {{
      "id": "npc_1",
      "name": "NPC名字",
      "description": "外貌和身份描述",
      "personality": "性格特点和行为方式",
      "background": "生平/来历：成长经历、与本案/其他角色的渊源等（KP 视角的背景，可含与剧情相关的过往；与 secrets 区分——background 是来历，secrets 是玩家不该直接知道的真相）",
      "secrets": ["只有KP知道的秘密"],
      "initial_location": "scene_1",
      "attributes": {{"STR": 50, "CON": 55, "SIZ": 60, "DEX": 50, "APP": 50, "INT": 70, "POW": 55, "EDU": 65, "LUCK": 50}},
      "skills": {{"战斗": 55, "闪避": 40, "侦查": 60, "潜行": 50, "心理学": 45}},
      "states": [
        {{"when": ["剧情标志名，如 butler_exposed"], "personality": "切换后的态度", "initial_location": "切换后的位置", "alive": true}}
      ]
    }}
  ],
  "triggers": [
    {{
      "id": "trig_1",
      "when": "用自然语言描述触发条件，如『玩家弄塌地下室水管』『管家的秘密被当面揭穿』『某 NPC 被杀』",
      "set_flags": ["该转折发生后应置上的剧情标志名"],
      "clear_flags": [],
      "description": "（可选）这一步剧情推进的简述"
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
  ],
  "handouts": [
    {{
      "id": "handout_1",
      "title": "手书标题，如 玛丽的遗书、阿卡姆广告报头版",
      "kind": "类型，仅限四选一：letter（信件/遗书/电报）/news（报纸/剪报/公告）/diary（日记/手记/笔记本）/note（便条/名片/收据/铭文等其他文书）",
      "content": "手书正文，**必须逐字保留模组原文**（含排版换行），绝对不要改写、缩写或润色",
      "location": "scene_1",
      "trigger_condition": "玩家如何拿到这份手书（如 搜查书房抽屉、验尸后从口袋发现）"
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
10. 时间线/剧情推进（重要）：模组里"会随剧情改变"的场景/NPC，用 states + triggers 表达，不要假设场景危险度一成不变：
    - 只为**确实会随剧情变化**的场景/NPC 写 states（变体），其余场景/NPC 的 states 留空数组 []；
      变体 when 引用剧情标志名，命中后覆盖对应字段（场景的 danger/atmosphere/description；NPC 的
      personality/initial_location/alive 等）。典型如「地下室进水后由 calm 变 deadly」「管家暴露后从谦卑变敌对并转移位置」。
    - 场景 state 若**改变了物理布局**（打破/打通墙、坍塌、进水淹没、露出新房间/暗格等），标 "structural": true；
      仅氛围/危险度变化（不动布局）则为 false。系统会为 structural=true 的状态**自动生成对应的变体地图**。
    - triggers 列出"何时该置/清哪个标志"：when 用自然语言写触发条件，set_flags/clear_flags 写标志名。
    - **标志名必须前后一致呼应**：triggers.set_flags 用到的标志，要在某场景/NPC 的 states.when 里被消费；
      反之 states.when 引用的标志，应有某个 trigger 负责置上。没有任何随剧情变化的内容时，triggers 留空数组 []。
11. 每个 NPC 给出 attributes（CoC 九维 STR/CON/SIZ/DEX/APP/INT/POW/EDU/LUCK，0-90 整数，按身份合理估计）
    与 background（生平来历）：attributes 供战斗/属性对抗与派生值使用；background 写来历渊源，与 secrets 区分。
12. handouts 只收模组原文**给出了完整正文**的文书（信件/报纸/日记/便条等「递给玩家看的实体道具」）：
    content 逐字照抄原文，一个字都不许改；原文只是提到某文书而没给正文的，不收。
    与 clues 的关系：手书本身可同时是线索——照常在 clues 里登记该线索，handouts 里存其原文正文，两者 id 各自独立。
    模组没有此类文书时 handouts 留空数组 []。

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


def _extract_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    return json.loads(s[a:b + 1])


async def parse_module_images(images: list[tuple[bytes, str]], rule_system: str, extra_text: str = "") -> dict:
    """多模态：据模组的图片（扫描页/图文模组）识别提取结构化数据（需视觉 LLM）。"""
    import base64
    llm = get_llm()
    if not llm.supports_vision():
        raise ValueError("当前模型不支持图片解析。请在设置里切换到支持视觉的模型（如 GPT-4o / Claude / Gemini / Qwen-VL），或上传文字版模组。")
    content = extra_text.strip() or "（模组内容见所附图片，请仔细阅读图片中的文字与示意图后提取）"
    prompt = PARSE_PROMPT_TEMPLATE.format(rule_system=rule_system.upper(), content=content)
    imgs = [(base64.b64encode(b).decode(), mime) for b, mime in images]
    raw = await llm.complete_vision(prompt, imgs, max_tokens=8192)
    return _extract_json(raw)


def create_module(db: Session, data: dict, raw_content: str = "") -> Module:
    world_setting = data.get("world_setting", {})
    for key in ("player_count", "era", "region", "difficulty", "tags", "player_brief", "intro"):
        if key in data:
            world_setting[key] = data[key]
    # 难度归一到枚举：非法值置空，避免脏数据进入筛选维度
    if world_setting.get("difficulty") not in MODULE_DIFFICULTIES:
        world_setting["difficulty"] = ""

    module = Module(
        title=data.get("title", "未命名模组"),
        rule_system=data.get("rule_system", "coc"),
        description=data.get("description", ""),
        world_setting=world_setting,
        raw_content=raw_content,
        scenes=data.get("scenes", []),
        npcs=data.get("npcs", []),
        clues=data.get("clues", []),
        triggers=data.get("triggers", []),
        handouts=data.get("handouts", []),
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
        ws = dict(data["world_setting"])
        if ws.get("difficulty") not in MODULE_DIFFICULTIES:
            ws["difficulty"] = ""
        module.world_setting = ws
    if "scenes" in data and data["scenes"] is not None:
        module.scenes = data["scenes"]
    if "npcs" in data and data["npcs"] is not None:
        module.npcs = data["npcs"]
    if "clues" in data and data["clues"] is not None:
        module.clues = data["clues"]
    if "triggers" in data and data["triggers"] is not None:
        module.triggers = data["triggers"]
    if "handouts" in data and data["handouts"] is not None:
        module.handouts = data["handouts"]
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
    # 显式删原文切块（SQLite 默认不强制级联，且测试库未必开外键），与规则书删除同理
    from app.models.module import ModuleChunk

    db.query(ModuleChunk).filter(ModuleChunk.module_id == module_id).delete()
    db.delete(module)
    db.commit()
    return True
