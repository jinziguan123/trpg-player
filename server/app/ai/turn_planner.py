from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.ai import director_signals
from app.ai.context import _active_flags, _resolve_state
from app.models import Character, GameSession, Module
from app.services import world_memory

logger = logging.getLogger(__name__)


TurnKind = Literal[
    "investigate",
    "social",
    "move",
    "combat",
    "knowledge",
    "roleplay",
    "mixed",
]


class CheckPlan(BaseModel):
    skill: str = ""
    difficulty: str = "normal"
    visibility: str = "open"
    reason: str = ""
    bonus: int = 0    # 奖励骰数量：情境明显有利时 1，系统多掷十位取优
    penalty: int = 0  # 惩罚骰数量：情境明显不利时 1，系统多掷十位取劣


class CluePolicy(BaseModel):
    action_matches_clue: bool = False
    candidate_clue_ids: list[str] = Field(default_factory=list)
    reveal_level: str = "none"
    requires_inspiration: bool = False
    notes: str = ""


class NpcPolicy(BaseModel):
    speakers: list[str] = Field(default_factory=list)
    reaction: str = ""
    needs_npc_act: bool = False


class ScenePolicy(BaseModel):
    scene_change: str | None = None
    set_flags: list[str] = Field(default_factory=list)
    clear_flags: list[str] = Field(default_factory=list)


class SafetyPolicy(BaseModel):
    do_not_reveal: list[str] = Field(default_factory=list)
    do_not_control_players: bool = True


class DirectionPolicy(BaseModel):
    """导演层：本轮的节奏经营意图。只影响「怎么讲」，不改变世界状态。

    ``direction`` 是软字段，模型常不严格照 schema（pacing 写成整句、spotlight 写成字符串）。
    这里做宽容归一——绝不能因为这个次要字段格式不对，就让整份 TurnPlan（含 clue_policy/
    safety/检定裁定等核心内容）校验失败被整体丢弃。识别不了的一律退到中性默认。
    """

    pacing: Literal["hold", "tighten", "release"] = "hold"
    spotlight: list[str] = Field(default_factory=list)  # 本轮应主动给戏份的角色名
    nudge: str = ""  # 卡关时的推进手段（让线索更显眼/NPC 主动接触），不得直接判定检定成功
    foreshadow: str = ""  # 建议埋设或回收的悬念，一句话

    @field_validator("pacing", mode="before")
    @classmethod
    def _coerce_pacing(cls, v):
        if not isinstance(v, str):
            return "hold"
        s = v.strip()
        if s in ("hold", "tighten", "release"):
            return s
        # 模型常写中文/整句：按关键词粗映射，识别不了就中性 hold
        if any(w in s for w in ("收紧", "推进", "加快", "加速", "升温", "紧凑", "紧张")):
            return "tighten"
        if any(w in s for w in ("放松", "放缓", "换气", "舒缓", "降温", "缓和")):
            return "release"
        return "hold"

    @field_validator("spotlight", mode="before")
    @classmethod
    def _coerce_spotlight(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            s = v.strip()
            return [s] if s else []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    @field_validator("nudge", "foreshadow", mode="before")
    @classmethod
    def _coerce_text(cls, v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return "；".join(str(x).strip() for x in v if str(x).strip())
        return str(v)


# 各嵌套子模型字段：LLM 常把它们写成一句话（safety→「安全，无即时威胁」、check→「不需要」），
# 形状错误只应让该字段退到默认，绝不能连累整份计划被丢弃回退旧流程。
_SUBMODEL_FIELDS = ("check", "clue_policy", "npc_policy", "scene_policy", "safety", "direction")
_LIST_FIELDS = ("narration_brief",)
_TURN_KINDS = frozenset(
    ("investigate", "social", "move", "combat", "knowledge", "roleplay", "mixed")
)


class TurnPlan(BaseModel):
    turn_kind: TurnKind = "mixed"
    player_intent: str = ""
    requires_check: bool = False
    check: CheckPlan = Field(default_factory=CheckPlan)
    clue_policy: CluePolicy = Field(default_factory=CluePolicy)
    npc_policy: NpcPolicy = Field(default_factory=NpcPolicy)
    scene_policy: ScenePolicy = Field(default_factory=ScenePolicy)
    narration_brief: list[str] = Field(default_factory=list)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    direction: DirectionPolicy = Field(default_factory=DirectionPolicy)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_wrong_shapes(cls, data):
        """把 LLM 写错形状的字段就地归一，保住整份计划不因次要字段格式错误被整体丢弃。

        - 嵌套子模型字段给了非 dict（一句话/标量）→ 换成 {}，走该子模型默认
          （子模型自身的 field_validator，如 direction 的 pacing/spotlight 归一，仍会生效）；
        - 列表字段给了字符串 → 包一层，其它非 list → 空列表；
        - turn_kind 给了枚举外的值 → 退到 mixed。
        识别不了的一律退默认，绝不抛错。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name in _SUBMODEL_FIELDS:
            # 放行 dict（来自 JSON）与子模型实例（来自直接构造）；只拦截标量/字符串/列表等错误形状
            if name in data and not isinstance(data[name], (dict, BaseModel)):
                data[name] = {}
        for name in _LIST_FIELDS:
            if name in data and not isinstance(data[name], list):
                v = data[name]
                data[name] = [str(v).strip()] if isinstance(v, str) and v.strip() else []
        if data.get("turn_kind") not in _TURN_KINDS:
            data.pop("turn_kind", None)  # 交回默认 "mixed"
        return data


def _visible_scene_ids(session: GameSession) -> set[str]:
    world_state = session.world_state or {}
    visible = set(world_state.get("visited_scenes") or [])
    if session.current_scene_id:
        visible.add(session.current_scene_id)
    return visible


def _filter_visible_items(items: list[dict] | None, visible_scene_ids: set[str]) -> list[dict]:
    if not items:
        return []
    result = []
    for item in items:
        location = item.get("location") or item.get("initial_location")
        if location and location not in visible_scene_ids:
            continue
        result.append(item)
    return result


def _compact_player(character: Character) -> dict[str, Any]:
    return {
        "id": character.id,
        "name": character.name,
        "rule_system": character.rule_system,
        "status": character.status,
        "skills": character.skills or {},
        "base_attributes": character.base_attributes or {},
    }


def _compact_events(events: list[Any]) -> list[dict[str, Any]]:
    compacted = []
    for event in events[-8:]:
        compacted.append({
            "type": getattr(event, "event_type", None) or getattr(event, "type", None),
            "speaker": getattr(event, "actor_name", None) or getattr(event, "speaker", None),
            "content": getattr(event, "content", "") or "",
        })
    return compacted


def build_turn_plan_messages(
    session: GameSession,
    module: Module,
    player_char: Character,
    events: list[Any],
    teammates: list[Character] | None = None,
    rules_lookup_enabled: bool = False,
) -> list[dict]:
    """构建 KP 回合规划器消息。

    规划器需要看到线索触发条件，但仍遵守运行时可见场景边界，避免提前读取
    玩家尚未到达区域的线索；场景/NPC 先按已激活 flags 解析成「当前样貌」——
    与 ``build_kp_context`` 用同一套 ``_active_flags``/``_resolve_state``，
    避免 planner 看到的画像和 KP 实际收到的不一致（如 NPC 位置/秘密因剧情变化）。
    """
    flags = _active_flags(session)
    resolved_scenes = [_resolve_state(scene, flags) for scene in (module.scenes or [])]
    # 模组 NPC + 已转正的临场 NPC 一并作为正典，进 visible_npcs / canonical_npcs
    _npc_defs = (module.npcs or []) + world_memory.promoted_npc_cards(session.world_state or {})
    resolved_npcs = [_resolve_state(npc, flags) for npc in _npc_defs]

    visible_ids = _visible_scene_ids(session)
    visible_clues = _filter_visible_items(module.clues, visible_ids)
    visible_npcs = _filter_visible_items(resolved_npcs, visible_ids)
    current_scene = next(
        (scene for scene in resolved_scenes if scene.get("id") == session.current_scene_id),
        None,
    )
    teammates = teammates or []
    # 线索台账：已发现的线索不再是 candidate——known 的直接标记 discovered，
    # 并把台账整体给 planner 做 clue_policy 判断输入（partial 的可升级为完整揭示）。
    clue_ledger = world_memory.discovered_clue_status(session.world_state or {})
    payload = {
        "module": {
            "title": module.title,
            "rule_system": module.rule_system,
            "description": module.description,
        },
        "session": {
            "current_scene_id": session.current_scene_id,
            "world_state": session.world_state or {},
            "rules_lookup_enabled": rules_lookup_enabled,
        },
        "current_scene": current_scene,
        "player": _compact_player(player_char),
        "teammates": [_compact_player(teammate) for teammate in teammates],
        "recent_events": _compact_events(events),
        "visible_npcs": [
            {
                "id": npc.get("id", ""),
                "name": npc.get("name", ""),
                "description": npc.get("description", ""),
                "personality": npc.get("personality", ""),
                "secrets": npc.get("secrets", []),
                "location": npc.get("location") or npc.get("initial_location", ""),
            }
            for npc in visible_npcs
        ],
        "visible_clues": [
            {
                "id": clue.get("id", ""),
                "name": clue.get("name", ""),
                "description": clue.get("description", ""),
                "location": clue.get("location", ""),
                "trigger_condition": clue.get("trigger_condition", ""),
                "discovered": bool(
                    clue.get("discovered", False)
                    or clue_ledger.get(clue.get("id", "")) == "known"
                ),
            }
            for clue in visible_clues
        ],
        "clue_ledger": clue_ledger,
        # 正典 NPC 名单（含已转正的临场 NPC；speakers/nudge 只能用这些名字）+
        # 未转正的临场龙套名单（不得带线索/推剧情）
        "canonical_npcs": [npc.get("name", "") for npc in visible_npcs if npc.get("name")],
        "improvised_npcs": [
            str(n).strip()
            for n, e in ((session.world_state or {}).get("improvised_npcs") or {}).items()
            if str(n).strip() and not (isinstance(e, dict) and (e.get("card") or {}).get("id"))
        ],
    }

    # 导演信号：确定性算出的节奏经营提示（冷场/卡关/单调/未解悬念），作为规划器输入。
    # 规划器据此产出 direction（怎么讲、给谁戏份、如何解卡），不影响世界状态。
    all_names = [player_char.name] + [t.name for t in teammates]
    signals = director_signals.compute_signals(
        events, module, session.world_state or {}, all_names,
    )
    director_block = ""
    if signals.has_actionable() or signals.unresolved_threads:
        director_block = (
            "\n\n导演信号（用于产出 direction 字段；这些只影响叙事节奏与戏份分配，"
            "绝不能凭此替玩家行动或直接判定检定成功）：\n" + signals.to_prompt()
        )

    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 的 KP 回合规划器。你的任务不是写叙事，而是先判断本轮裁定："
                "玩家意图、是否需要检定、可揭示线索、NPC 反应、场景变化、安全边界，"
                "以及导演层的节奏经营 direction。direction 的字段格式必须严格遵守："
                "pacing 只能是 \"hold\"/\"tighten\"/\"release\" 三者之一（不是句子）；"
                "spotlight 是角色名的**数组**（如 [\"伊芙琳\"]，无则 []）；"
                "nudge、foreshadow 是字符串（无则 \"\"）。"
                "只输出一个 JSON object，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请基于以下运行时资料生成本轮裁定计划。"
                "线索只有在玩家行动匹配 trigger_condition 时才可进入 candidate_clue_ids。"
                "不得使用 visible_clues 以外的线索。"
                "clue_ledger 是玩家已掌握线索的台账：status=known（或 discovered=true）"
                "的线索已完全揭示，不得再进入 candidate_clue_ids；"
                "status=partial 的仅在玩家行动继续深入时可作为升级揭示的 candidate。"
                "check.skill 除技能名外也可以是九维属性中文名"
                "（力量/体质/体型/敏捷/外貌/智力/意志/教育/幸运；灵感=智力、知识=教育）——"
                "玩家行动没有贴切技能时选最相关的属性。"
                "导演信号显示卡关时，应主动裁定一次灵感/教育/相关知识检定作为解卡手段"
                "（requires_check=true，并让 direction.nudge 与之呼应：成功给一点方向、"
                "失败不给或给误导），不要干等玩家自己想起来申请。"
                "但主动裁定仅限被动/本能类（感知/抗性/灵光/SAN）；心理学、话术、图书馆使用等"
                "**主动运用型技能**只能因应玩家自己的宣言裁定，玩家没说要用就不发——那是替玩家行动。\n"
                "npc_policy.speakers 与 direction.nudge 里的 NPC **只能用 canonical_npcs 里的名字**；"
                "improvised_npcs 是 KP 此前临场添加的龙套——**绝不安排他们携带线索、透露情报或推动剧情**，"
                "最多作为氛围出现，追问时指回模组内容。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + director_block
            ),
        },
    ]


def _extract_json_object(raw: Any) -> dict | None:
    """从 LLM 原始输出里稳健地抠出一个 JSON object。

    模型常不严格遵守 ``response_format=json_object``：可能已是 dict、被 ```json 围栏包裹、
    或在 JSON 前后夹带解释文字。依次尝试：直接用 dict → 剥围栏后整体解析 → 抠出首个 ``{``
    到末个 ``}`` 的子串解析。都不成返回 None，由调用方回退旧流程。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # 去掉 ```json ... ``` / ``` ... ``` 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 前后夹带了解释文字：抠出最外层大括号范围再试
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


async def run_turn_planner(llm: Any, messages: list[dict]) -> TurnPlan | None:
    try:
        # 不设 max_tokens 硬上限：推理类模型的 reasoning 会占输出预算，硬上限（原为 1200）会让
        # content 被 reasoning 耗空、返回空串（表现为「原始片段为空」）。交由服务端默认上限。
        raw = await llm.complete(
            messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("KP 回合规划器调用失败，已回退旧流程")
        return None

    data = _extract_json_object(raw)
    if data is None:
        snippet = str(raw)[:200]
        if not snippet.strip():
            logger.warning(
                "KP 回合规划器返回空内容，已回退旧流程（多为推理模型预算被 reasoning 耗尽，"
                "或供应商异常）",
            )
        else:
            logger.warning(
                "KP 回合规划器输出无法解析为 JSON，已回退旧流程；原始片段：%s", snippet,
            )
        return None
    try:
        return TurnPlan.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        logger.warning("KP 回合规划器 JSON 不符合 schema，已回退旧流程：%s", exc)
        return None


# 供 KPAgent 识别「本轮是必发检定的裁定轮」的稳定标记：出现在注入消息里即代表本轮
# requires_check=true。检定轮对「只写尝试、以指令收尾、不提前泄结果」的服从度要求极高，
# 高温采样会让模型忍不住把「敲出空响、摸到暗缝」写出来——故 KP 见此标记时压低采样温度，
# 让指令遵循压过创造性发挥。改这里的字面量要同步 KPAgent。
REQUIRES_CHECK_MARKER = "【本轮必须发起检定"


def _check_directive(check: CheckPlan) -> str:
    """按计划里的 check 拼出本轮必须发的 [DICE_CHECK] 指令原文，直接喂给 KP 照发。"""
    parts = [f"skill={check.skill or '侦查'}"]
    if check.difficulty:
        parts.append(f"difficulty={check.difficulty}")
    if check.visibility and check.visibility != "open":
        parts.append(f"visibility={check.visibility}")
    if check.bonus:
        parts.append(f"bonus={check.bonus}")
    if check.penalty:
        parts.append(f"penalty={check.penalty}")
    return f"[DICE_CHECK: {', '.join(parts)}]"


def build_turn_plan_message(plan: TurnPlan) -> dict:
    content = {
        "turn_kind": plan.turn_kind,
        "player_intent": plan.player_intent,
        "requires_check": plan.requires_check,
        "check": plan.check.model_dump(),
        "clue_policy": plan.clue_policy.model_dump(),
        "npc_policy": plan.npc_policy.model_dump(),
        "scene_policy": plan.scene_policy.model_dump(),
        "narration_brief": plan.narration_brief,
        "safety": plan.safety.model_dump(),
        "direction": plan.direction.model_dump(),
    }

    # requires_check=true 时，把「必须发检定、且发之前不许泄结果/线索位置」写成不可绕过的硬约束，
    # 单独成段、给出照发的指令原文——否则模型容易把动作叙述「讲完」（敲出空层、摸到暗缝），
    # 既不发指令又提前泄露了本该靠检定才发现的线索存在与位置。这是本文件评估回路里
    # plan_adherence 连续不过的根因。
    check_block = ""
    if plan.requires_check:
        directive = _check_directive(plan.check)
        check_block = (
            "\n\n----------------------------------------\n"
            + REQUIRES_CHECK_MARKER
            + "——最高优先级硬约束，凌驾叙事完整性，违反即为严重错误】\n"
            "本轮 requires_check=true。你这次回复的唯一正确结束方式，是原样输出下面这行检定指令"
            "并就此停笔——把它当作你回复的**最后一行**逐字照抄，一个字都不要改：\n"
            f"    {directive}\n"
            "（skill/difficulty/visibility 一律照计划 check 字段，不得改动、不得省略、不得替换成别的技能。）\n"
            "\n硬性要求，逐条遵守：\n"
            "1. 指令之后不许有任何文字；这一整段回复里，指令**之前**也**绝不允许**写出或暗示检定结果——"
            "不得叙述玩家已经「找到 / 发现 / 摸到 / 听到 / 看懂 / 察觉 / 注意到」任何东西，"
            "更不得点出线索的存在、位置或形态（例如「某处回音发空」「摸到一条暗缝」"
            "「那块木板不对劲」「有什么东西藏在里面」「似乎是空的」都属于提前泄露，一律禁止）。\n"
            "2. 你**只能**描写玩家「正在尝试」的过程动作本身（俯身、伸手、指节叩击、逐寸摸索），"
            "以及与答案无关的固定环境（家具材质、房间光线气味）。**特别注意**：叩击/触摸所得到的"
            "「反馈」本身就是检定要揭晓的答案——绝不能描述这次叩击「回音发空 / 声音不同 / 某处发虚」，"
            "也不能描述摸到「接缝 / 细缝 / 松动 / 空腔」；这些哪怕写得再含蓄，都等于替检定给出了结果。"
            "把「敲/摸到底反馈出了什么」完全留给检定结果去揭晓。\n"
            "3. 哪怕这样叙事看起来「没讲完」「戛然而止」，也必须就此以该指令收尾——"
            "发起检定本身就是本轮的正确收束；不要为了把动作叙述写「完整」而抢先给出结果或线索，"
            "也不要用「你开始仔细检查……」这类没有指令的句子代替它。\n"
            "4. narration_brief 里若有「描写反馈 / 声音 / 反应」之类措辞，只表示要渲染尝试当下的"
            "**中性**氛围，绝不是允许你写出检定的答案（如「回音发空」「像是空的」＝答案，禁止）；"
            "本硬约束的优先级高于 narration_brief 与任何叙事完整性的考量。\n"
            "\n对照示例（务必学会「在动作抬手处就收尾发指令」）：\n"
            "  【错误写法】（写出了叩击的反馈/发现、且没发指令）：「……你敲到侧板下方，回音发空，"
            "摸到一条暗缝，你准备进一步探查这处可疑的地方。」\n"
            f"  【正确写法】：「你俯身，借着窗外的天光凑近那张深色橡木书桌，指节沿着侧板逐寸叩下、"
            f"再用指腹贴着雕花细细摸索。」换行，然后最后一行写：{directive}\n"
            "注意：正确写法只写到「玩家伸手去敲/去摸」的动作就停住并发指令，绝不写这一敲/一摸「反馈出了什么」"
            "（不写回响是否发空、不写有没有缝隙）。\n"
            "再强调一次：本次回复务必以这一行结束，且这必须是回复真正的最后一行 —— " + directive + "\n"
        )

    # check_block 放在 JSON 之后收尾：模型对「上下文最末尾的指令」权重最高，把这条硬约束
    # 作为最后读到的内容，能显著提升「照发 [DICE_CHECK] 收尾、不提前泄结果」的遵循率。
    return {
        "role": "system",
        "content": (
            "【本轮裁定计划】（内部工作稿，仅供你裁定参考——不是要念给玩家听的内容）\n"
            "你必须据此计划生成本回合叙事和内部指令，但绝不能把下面 JSON 的字段名、结构、"
            "或 flag/线索/NPC 的内部 id 等技术性标识，以任何形式（复述、总结、列表、标题）"
            "写进给玩家看的文本；看到这份结构化计划**不代表要改用「汇报体」输出**——回复必须"
            "仍是紧贴情境的自然语言叙事，不得另起标题分段或项目符号列表汇报状态。\n"
            "safety.do_not_reveal 的内容不能通过任何暗示性总结泄露。\n"
            "direction 是导演笔记（内部指引，严禁向玩家复述原文）：pacing 是本轮节奏"
            "（tighten=收紧推进/release=放松换气/hold=保持）；spotlight 列出的角色本轮要给戏份，"
            "**但只能通过环境（让某物朝他显现/异动）、NPC 主动看他说他、或把机会摆到他面前来给**——"
            "**绝对不许替 spotlight 里的玩家角色描写任何动作、姿态、心理或台词**（那是替玩家行动，"
            "凌驾于给戏份之上的最高禁令）；本轮只有实际发出行动的玩家角色才可被叙述其尝试过程，"
            "其他玩家角色一律不替其行动。nudge 是解卡手段，只能让线索更显眼或让 NPC 主动接触，"
            "绝不能替玩家决定或直接宣布检定成功；foreshadow 是可择机埋设/回收的悬念。\n"
            + json.dumps(content, ensure_ascii=False, indent=2)
            + check_block
        ),
    }
