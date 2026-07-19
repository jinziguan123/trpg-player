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
  "truth": "幕后真相（守秘人资讯）：整个事件**真正发生了什么**——幕后黑手/元凶是谁、动机为何、按时间顺序的来龙去脉、各 NPC 在其中扮演的角色、玩家介入时局面处于哪一步。模组开头的『守秘人资讯/背景真相/KP须知』一类章节要**完整浓缩收录于此**（可以多段，宁全勿缺）。这是 KP 专属参考，玩家永远不可见。模组没有此类内容时留空字符串",
  "scenes": [
    {{
      "id": "scene_1",
      "title": "场景标题",
      "description": "场景详细描述",
      "danger": "该场景的危险等级，仅限四选一：calm（安全平静）/uneasy（隐隐不安）/dangerous（明确危险）/deadly（致命凶险）",
      "atmosphere": "一句话氛围基调，给 KP 渲染用：以感官（声/味/光/体感）+ 情绪基调描述，如『腐臭、低压、木板随时塌陷』。不要写成剧透或台词",
      "kind": "二选一：location（一个真实存在的地点，默认）/ chapter（纯叙事章节或抽象阶段，如『委托与准备』『尾声』——它不是玩家能在地图上前往的地方）",
      "keywords": ["解锁关键词：玩家在对话/行动里提到其中任意一个，大地图就解锁该地点，因此**每个词都必须是『这个地点的称呼』**。覆盖：完整地名、核心地名（去掉『废墟/遗址/旧址』等状态词，如『沉思礼拜堂废墟』→『沉思礼拜堂』）、通俗设施名（礼拜堂/图书馆）、专名（沉思/科比特/罗克斯伯里）、模组原文里的门牌地址或俗称/绰号，以及数字写法变体（『2号车厢』要含『二号车厢』）。**绝不要该场景的内容词**：场景里的物件（行李/钥匙/报纸）、人物或怪物（乘务员/循声者）、氛围描写（黑暗/血腥/喘息）都不是地点称呼——这类词一旦出现在任何叙述里就会误解锁该地点、提前剧透。2-6 个，每个≥2字；不要过泛的通用词（如『房间』『那边』『房子』『街区』）。chapter 类场景留空数组"],
      "connections": ["scene_2"],
      "events": [
        {{"trigger": "触发情景，自然语言：进入场景即目睹/翻动尸体/打开衣柜/点灯后……", "kind": "四选一：san_check（见恐怖景象掷理智）/dice_check（需技能检定）/damage（陷阱或环境伤害）/note（其他机制性提示）", "san_loss": "kind=san_check 时的损失规格，**照抄模组原文**（如 0/1d3、1/1d6+1）", "skill": "kind=dice_check 时的技能名", "damage": "kind=damage 时的伤害骰式（如 1d6）", "note": "补充说明或后果"}}
      ],
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
      "hp": 11,
      "armor": 0,
      "weapon": "主要攻击方式/武器名（如 匕首、猎枪、撕咬；徒手可省略）",
      "goals": ["该 NPC 的目标/动机：他接下来想达成什么（玩家不在场时他会朝这个方向行动）"],
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
4. 场景的 connections 标明**物理上直接相连、一步可达**的场景（有门/通道/楼梯直通）——
   系统会按这张图硬性限制移动：不相连就到不了，隔着中间场景就必须途经。
   线性结构（列车车厢、隧道、楼层）必须严格按空间顺序相连（6号车厢只连 7号和 5号，
   绝不能直连 2号）；不要因为「剧情上先后发生」或场景编号相邻就连边
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
13. 每个 location 类场景给出 keywords（解锁关键词，2-6 个）：玩家提到任一即在大地图解锁该地点，
    因此**每个词都必须是『这个地点的称呼』**：完整地名、剥掉『废墟/遗址/旧址』等状态词的核心地名
    （『沉思礼拜堂废墟』要含『沉思礼拜堂』与『礼拜堂』）、门牌地址、俗称/绰号、数字写法变体
    （『2号车厢』要含『二号车厢』）。**绝不要该场景的内容词**——物件、人物/怪物、氛围描写
    （行李/钥匙/乘务员/怪物/黑暗/血腥等）都不是地点称呼，出现在任何叙述里就会误解锁、提前剧透；
    也避免『房间』『房子』『街区』这类过泛通用词。chapter 类场景 keywords 留空数组 []。
14. truth（幕后真相）**宁全勿缺**：模组的守秘人资讯是 KP 运转的根基，凡「真正发生了什么」的
    叙述都要收进去；它与 NPC 的 secrets 不冲突（secrets 是单个 NPC 的秘密，truth 是全局真相）。
15. 场景 events 只收模组**明文规定**的机制点（原文写了「目睹 X 需 0/1d3 理智检定」「触碰 Y 受
    1d6 伤害」之类）：数值一律照抄原文，绝不自行估值；模组没写的不要编造。无机制点留空数组 []。
16. NPC/怪物给出 hp（原文数值；没有则按 (CON+SIZ)/10 估算）、armor（护甲值，无甲为 0）、
    weapon（主要攻击方式：人类用武器名，怪物用其攻击方式名如『撕咬』『触手』）——供战斗引擎
    直接使用；goals 写他接下来想达成什么（幕后推演据此让世界在玩家不在场时演进）。

模组文本：
{content}"""


async def parse_module_text(raw_text: str, rule_system: str, on_progress=None) -> dict:
    """用 AI 解析模组文本为结构化数据。

    大模组的输出 JSON 很长（场景 keywords/connections/states + NPC 技能 + 手书逐字正文），
    单次 completion 撞到 max_tokens 会被拦腰截断成坏 JSON。检测到截断时**自动续写一次**：
    把半截输出作为 assistant 上文让模型从断点接着写，拼接后再解析——输出预算等效翻倍，
    且不依赖任何供应商的超大 max_tokens。仍失败才抛给上层（上传任务落成可读失败信息）。

    ``on_progress``：可选进度回调（进入断点续写等子阶段时以一句话汇报，供上传进度条展示）。
    """
    llm = get_llm()
    prompt = PARSE_PROMPT_TEMPLATE.format(
        rule_system=rule_system.upper(), content=raw_text
    )
    messages = [{"role": "user", "content": prompt}]

    result = await llm.complete(
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    try:
        return _extract_json(result)
    except json.JSONDecodeError:
        logger.warning(
            "模组解析 JSON 不完整（长度 %d，尾部 %r），尝试断点续写",
            len(result or ""), (result or "")[-120:],
        )
    if on_progress is not None:
        try:
            on_progress("输出超长被截断，正在断点续写恢复…")
        except Exception:  # noqa: BLE001 — 进度汇报绝不影响解析
            pass

    # 续写不带 response_format=json_object：那会迫使模型重开一个全新 JSON，而不是接着写
    continuation = await llm.complete(
        messages=messages + [
            {"role": "assistant", "content": result},
            {"role": "user", "content": (
                "你的 JSON 输出到上面为止被截断了。请**从断点处直接继续**：接着上文的"
                "最后一个字符往下写，补完整个 JSON。不要重复任何已输出的内容、"
                "不要解释、不要 markdown 围栏。"
            )},
        ],
        temperature=0.3,
    )
    combined = (result or "") + (continuation or "")
    # 优先按「断点拼接」解析；个别模型不接续而是整个重output——退而解析续写单独成篇的情形
    for candidate in (combined, continuation or ""):
        try:
            parsed = _extract_json(candidate)
            logger.info("模组解析断点续写成功（总长 %d）", len(candidate))
            return parsed
        except json.JSONDecodeError:
            continue
    return _extract_json(combined)  # 仍不完整：抛 JSONDecodeError，由上层回可读 502


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
    raw = await llm.complete_vision(prompt, imgs)
    return _extract_json(raw)


SUPPLEMENT_PROMPT_TEMPLATE = """你是 {rule_system} 模组解析的质检员。下面给出模组原文与首轮解析出的结构化 JSON。
请**逐段对照原文**，找出首轮解析**遗漏**的重要内容，只输出一个 JSON 对象（不要解释）：

{{
  "truth": "首轮 truth 遗漏的幕后真相补充（真凶/动机/时间线/来龙去脉）；已收录完整则空字符串",
  "scenes": ["仅两种条目：①整个被遗漏的场景（完整场景对象，字段同首轮）；②已有场景遗漏了机制点时，给 {{\\"id\\": \\"已有场景id\\", \\"events\\": [仅遗漏的机制点]}}——events 数值照抄原文（如 0/1d3）"],
  "npcs": ["仅两种条目：①整个被遗漏的 NPC/怪物（完整对象，含 attributes/skills/hp/armor/weapon/goals）；②已有 NPC 遗漏关键字段时，给 {{\\"id\\": \\"已有id\\", 仅补缺的字段}}"],
  "clues": ["仅整个被遗漏的线索（完整对象）"],
  "handouts": ["仅整个被遗漏的手书（完整对象，content 逐字照抄原文）"]
}}

铁律：只补遗漏，**绝不重复、改写或删改已收录的内容**；没有遗漏就输出全空（空串/空数组）。
重点排查：守秘人资讯/背景真相章节、进入场景或特定行动触发的理智检定与伤害（数值照抄）、
怪物资料（hp/护甲/攻击方式）、被跳过的场景或 NPC、给了完整正文却没收的手书。

【模组原文】
{content}

【首轮解析 JSON】
{parsed}"""


def _merge_supplement(parsed: dict, patch: dict) -> dict:
    """把查漏自检的补丁**保守合并**进首轮解析结果（纯函数，不改入参）。

    - truth：首轮为空则取补丁；两者都有且补丁不是重复内容则追加；
    - scenes/npcs：新 id 追加；已有 id 只做增量——场景合并遗漏的 events（按 trigger 去重），
      NPC 只填首轮**缺失/为空**的字段（绝不覆盖已有值）；
    - clues/handouts：新 id 追加，已有 id 忽略（不允许改写）。
    """
    out = dict(parsed or {})

    p_truth = str(out.get("truth") or "").strip()
    n_truth = str((patch or {}).get("truth") or "").strip()
    if n_truth and not p_truth:
        out["truth"] = n_truth
    elif n_truth and n_truth not in p_truth:
        out["truth"] = p_truth + "\n\n【查漏补充】" + n_truth

    def _by_id(items):
        return {str(x.get("id")): x for x in (items or []) if isinstance(x, dict) and x.get("id")}

    # scenes：新场景追加；已有场景合并遗漏 events
    scenes = [dict(s) for s in (out.get("scenes") or [])]
    have = _by_id(scenes)
    for item in (patch or {}).get("scenes") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        sid = str(item["id"])
        if sid not in have:
            scenes.append(item)
            continue
        target = next(s for s in scenes if str(s.get("id")) == sid)
        seen = {str((e or {}).get("trigger") or "").strip() for e in (target.get("events") or [])}
        extra = [
            e for e in (item.get("events") or [])
            if isinstance(e, dict) and str(e.get("trigger") or "").strip() not in seen
        ]
        if extra:
            target["events"] = list(target.get("events") or []) + extra
    out["scenes"] = scenes

    # npcs：新 NPC 追加；已有 NPC 只填缺失字段（列表字段追加去重）
    npcs = [dict(n) for n in (out.get("npcs") or [])]
    have = _by_id(npcs)
    for item in (patch or {}).get("npcs") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        nid = str(item["id"])
        if nid not in have:
            npcs.append(item)
            continue
        target = next(n for n in npcs if str(n.get("id")) == nid)
        for key, val in item.items():
            if key == "id" or val in (None, "", [], {}):
                continue
            cur = target.get(key)
            if isinstance(cur, list) and isinstance(val, list):
                target[key] = cur + [v for v in val if v not in cur]
            elif cur in (None, "", [], {}, 0) and key != "armor":  # armor=0 是合法值，不视为缺失
                target[key] = val
            elif key == "armor" and cur is None:
                target[key] = val
    out["npcs"] = npcs

    # clues / handouts：只追加新 id
    for key in ("clues", "handouts"):
        items = list(out.get(key) or [])
        have = _by_id(items)
        for item in (patch or {}).get(key) or []:
            if isinstance(item, dict) and item.get("id") and str(item["id"]) not in have:
                items.append(item)
        out[key] = items
    return out


async def supplement_parse(raw_text: str, parsed: dict, rule_system: str) -> dict:
    """查漏自检（P4）：把原文与首轮解析回喂一次，找出遗漏项并保守合并。

    fail-open：无原文（纯图片模组）/ LLM 异常 / 坏 JSON 一律原样返回首轮结果，绝不劣化。
    """
    if not (raw_text or "").strip():
        return parsed
    llm = get_llm()
    prompt = SUPPLEMENT_PROMPT_TEMPLATE.format(
        rule_system=rule_system.upper(),
        content=raw_text,
        parsed=json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
    )
    try:
        raw = await llm.complete(
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        patch = _extract_json(raw)
    except Exception:  # noqa: BLE001 — 自检是增强件，失败绝不拖垮导入
        logger.exception("模组查漏自检失败（跳过，沿用首轮解析结果）")
        return parsed
    added = {
        k: len(patch.get(k) or []) for k in ("scenes", "npcs", "clues", "handouts")
    }
    if any(added.values()) or (patch.get("truth") or "").strip():
        logger.info("模组查漏自检补充：truth=%s 增量=%s",
                    bool((patch.get("truth") or "").strip()), added)
    return _merge_supplement(parsed, patch)


def _ensure_scene_keywords(scenes: list) -> list:
    """给每个 location 场景补全解锁关键词：LLM 生成的 keywords ∪ 标题确定性派生（兜底），
    归一（去空白、去重、≥2字）。chapter 类不需要（不上地图）。解析与手动编辑都经此归一。"""
    from app.services.session_service import derive_scene_keywords

    for s in scenes or []:
        if not isinstance(s, dict) or s.get("kind") == "chapter":
            continue
        title = s.get("title") or s.get("name") or ""
        given = {
            k.strip() for k in (s.get("keywords") or [])
            if isinstance(k, str) and len(k.strip()) >= 2
        }
        s["keywords"] = sorted(given | derive_scene_keywords(title))
    return scenes


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
        scenes=_ensure_scene_keywords(data.get("scenes", [])),
        npcs=data.get("npcs", []),
        clues=data.get("clues", []),
        triggers=data.get("triggers", []),
        handouts=data.get("handouts", []),
        truth=str(data.get("truth") or ""),
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
        module.scenes = _ensure_scene_keywords(data["scenes"])
    if "npcs" in data and data["npcs"] is not None:
        module.npcs = data["npcs"]
    if "clues" in data and data["clues"] is not None:
        module.clues = data["clues"]
    if "triggers" in data and data["triggers"] is not None:
        module.triggers = data["triggers"]
    if "handouts" in data and data["handouts"] is not None:
        module.handouts = data["handouts"]
    if "truth" in data and data["truth"] is not None:
        module.truth = str(data["truth"])
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
