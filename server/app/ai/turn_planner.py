from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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


def _coerce_str_list(v: Any) -> list[str]:
    """把 LLM 常写错的列表字段就地归一成干净的 str 列表。

    模型时常把列表字段写成 ``null``（default_factory 只在键缺失时生效，显式 null 会撞
    schema）、一句话，或含空串/非字符串元素。统一收敛：None/无法识别→空列表，字符串→单元素，
    列表→逐项 str 化去空。绝不因这类次要字段格式错误就让整份 TurnPlan 校验失败回退旧流程。"""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


# 所有「字符串列表」软字段统一用它承接 LLM 的脏输入，避免每个字段各写一遍归一逻辑。
StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


def _coerce_scalar_text(v: Any) -> str:
    """把 LLM 误写成 dict/list/标量/None 的**自由文本字段**就地转成字符串，尽量保住内容
    （dict 取各值拼句、list 拼接），而不是丢成空串或让整份计划校验失败回退旧流程。

    典型：把一句话意图写成 {"actor": "江户川龙牙", "intent": "…驾驶车体"} → 「江户川龙牙；…驾驶车体」。"""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, dict):
        return "；".join(s for s in (str(x).strip() for x in v.values()) if s)
    if isinstance(v, (list, tuple)):
        return "；".join(s for s in (str(x).strip() for x in v) if s)
    return str(v)


def _coerce_str_fields(model_cls: type[BaseModel], data: dict) -> dict:
    """把 data 里所有「目标类型是纯 str」的字段的非字符串值就地转成字符串（通用容错）。

    模型反复把标量 str 字段（player_intent / auto_outcome_reason / 子模型的 reason/skill/…）写成
    dict/list/数字 → 撞 str 类型令**整份计划**被丢弃回退旧流程。用模型自身字段类型驱动，逐字段判定，
    枚举/列表/布尔字段（annotation 非纯 str）一律跳过，不误伤。"""
    for name, field in model_cls.model_fields.items():
        if field.annotation is str and name in data and not isinstance(data[name], str):
            data[name] = _coerce_scalar_text(data[name])
    return data


class CheckPlan(BaseModel):
    skill: str = ""
    difficulty: str = "normal"
    visibility: str = "open"
    chars: str = ""  # 群检范围："在场"/"全体" 或角色名单；空=主角
    reason: str = ""
    bonus: int = 0    # 奖励骰数量：情境明显有利时 1，系统多掷十位取优
    penalty: int = 0  # 惩罚骰数量：情境明显不利时 1，系统多掷十位取劣


class CluePolicy(BaseModel):
    action_matches_clue: bool = False
    candidate_clue_ids: StrList = Field(default_factory=list)
    reveal_level: str = "none"
    requires_inspiration: bool = False
    notes: str = ""


class NpcPolicy(BaseModel):
    speakers: StrList = Field(default_factory=list)
    reaction: str = ""
    needs_npc_act: bool = False


class ScenePolicy(BaseModel):
    scene_change: str | None = None
    set_flags: StrList = Field(default_factory=list)
    clear_flags: StrList = Field(default_factory=list)


class CombatPlan(BaseModel):
    """本轮是否必须从自由叙事切入结构化战斗。"""

    should_start: bool = False
    enemies: StrList = Field(default_factory=list)
    trigger: str = ""


class SafetyPolicy(BaseModel):
    do_not_reveal: StrList = Field(default_factory=list)
    do_not_control_players: bool = True


def _coerce_item_deltas(v: Any) -> list:
    """把物品增减列表归一：非 list→[]；字符串元素→{name}；dict 需有 name；
    已是 ItemDelta 实例（直接构造/内部传入）原样放行；其余丢弃。"""
    if not isinstance(v, list):
        return []
    out: list = []
    for x in v:
        if isinstance(x, BaseModel):
            out.append(x)
        elif isinstance(x, dict) and str(x.get("name") or "").strip():
            out.append(x)
        elif isinstance(x, str) and x.strip():
            out.append({"name": x.strip()})
    return out


class ItemDelta(BaseModel):
    """一件物品的获得/失去。who=获得者或失去者角色名（缺省=本轮行动的玩家）。"""

    name: str = ""
    qty: int = 1
    kind: str = ""   # 获得时可选 consumable/gear/key/document；失去时忽略
    who: str = ""


ItemDeltaList = Annotated[list[ItemDelta], BeforeValidator(_coerce_item_deltas)]


class CombatDamage(BaseModel):
    """战斗中一次非常规/范围攻击对敌人造成的伤害（燃烧弹/泼火/群体/环境）——引擎没建模的单体
    武器攻击之外的伤害，由此确定性落到敌人 HP，不靠 KP 叙述。命中已由先前的投掷检定判定。"""

    trigger: bool = False
    targets: StrList = Field(default_factory=list)  # 被波及的敌人名（战斗态里的名字）
    weapon: str = ""     # 已知投掷武器名（查武器表拿伤害/燃烧，如「莫洛托夫鸡尾酒」）
    formula: str = ""    # 武器表查不到时的伤害骰式（如「2d6」）
    burning: bool = False  # 命中后附加燃烧（每轮 1d6 直到扑灭）；武器表带「烧/燃烧」会自动置
    reason: str = ""


class SanityPolicy(BaseModel):
    """本轮是否有角色目睹/得知会动摇理智的恐怖——由 planner 裁定，引擎据此确定性发 SAN 检定，
    不依赖 KP 临场记得。trigger=False 时其余字段忽略。"""

    trigger: bool = False
    source: str = ""              # 恐怖源标识（去重键，如「墓室腐尸」）
    success_loss: str = "0"       # 成功损失（骰式/数字），按冲击程度：尸体 0、血腥/怪物 1、神话生物 1d6
    failure_loss: str = "1d6"     # 失败损失：尸体 1d3、血腥/怪物 1d6、强大神话生物 1d20
    witnesses: StrList = Field(default_factory=list)  # 目睹者名单（缺省=在场全体）

    @field_validator("success_loss", "failure_loss", mode="before")
    @classmethod
    def _coerce_loss(cls, v, info):
        """损失字段是「骰式/数字」，模型常直接写整数 0/1（int）——显式非字符串会撞 str 类型、
        令整份计划校验失败回退旧流程（丢掉全部裁定信号）。统一 str 化；None/空退回字段默认。"""
        default = "0" if info.field_name == "success_loss" else "1d6"
        if v is None:
            return default
        return str(v).strip() or default


class MishapPolicy(BaseModel):
    """本轮玩家掷出**大失败**、且所做动作本身有身体危险时，危险反噬自身造成的伤害——由 planner
    在检定后裁定，引擎确定性扣 HP，不依赖 KP 记得发 HP_CHANGE。trigger=False 或非危险动作时其余
    字段忽略（多数大失败并无身体伤害，如图书馆/话术检定失败）。仅大失败才可能触发。"""

    trigger: bool = False
    hp_delta: int = 0     # 扣血（负）：轻度反噬 -1~-3（烧灼/擦碰/割伤），重度 -4~-6（跌落/大面积灼伤）
    target: str = ""      # 受伤角色名（缺省=本轮掷骰的玩家）
    reason: str = ""      # 一句话缘由（如「踢翻的燃烧瓶溅到自己」）

    @field_validator("hp_delta", mode="before")
    @classmethod
    def _coerce_delta(cls, v):
        """hp_delta 恒为伤害（负整数）；模型可能写成 "-3"/正数/"1d3"/null。取整→强制取负→夹在 -8，
        取不到整数则 0（不伤）。绝不因这个字段格式不对而让整份计划校验失败回退旧流程。"""
        if isinstance(v, bool) or v is None:
            return 0
        try:
            n = int(v) if isinstance(v, (int, float)) else int(str(v).strip())
        except (ValueError, TypeError):
            return 0
        return max(-abs(n), -8)


class DirectionPolicy(BaseModel):
    """导演层：本轮的节奏经营意图。只影响「怎么讲」，不改变世界状态。

    ``direction`` 是软字段，模型常不严格照 schema（pacing 写成整句、spotlight 写成字符串）。
    这里做宽容归一——绝不能因为这个次要字段格式不对，就让整份 TurnPlan（含 clue_policy/
    safety/检定裁定等核心内容）校验失败被整体丢弃。识别不了的一律退到中性默认。
    """

    pacing: Literal["hold", "tighten", "release"] = "hold"
    spotlight: StrList = Field(default_factory=list)  # 本轮应主动给戏份的角色名
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
_SUBMODEL_FIELDS = (
    "check", "clue_policy", "npc_policy", "scene_policy", "combat", "combat_damage",
    "safety", "sanity", "mishap", "direction",
)
_TURN_KINDS = frozenset(
    ("investigate", "social", "move", "combat", "knowledge", "roleplay", "mixed")
)


class TurnPlan(BaseModel):
    turn_kind: TurnKind = "mixed"
    player_intent: str = ""
    requires_check: bool = False
    # 自动结局（虚构态势已让结果确定，无需掷骰）：none=照常裁定；success=直接成功（如现实话术
    # 精彩到位免检）；failure=直接失败（如已暴露却仍想潜行）。与 requires_check 互斥。
    auto_outcome: str = "none"
    auto_outcome_reason: str = ""   # 免检直接判定的**入戏理由**，供 KP 演出来（不照念字段）
    check: CheckPlan = Field(default_factory=CheckPlan)
    clue_policy: CluePolicy = Field(default_factory=CluePolicy)
    npc_policy: NpcPolicy = Field(default_factory=NpcPolicy)
    scene_policy: ScenePolicy = Field(default_factory=ScenePolicy)
    combat: CombatPlan = Field(default_factory=CombatPlan)
    narration_brief: StrList = Field(default_factory=list)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    sanity: SanityPolicy = Field(default_factory=SanityPolicy)
    mishap: MishapPolicy = Field(default_factory=MishapPolicy)  # 大失败的身体反噬伤害（确定性扣 HP）
    combat_damage: CombatDamage = Field(default_factory=CombatDamage)  # 战斗中非常规/范围攻击伤害
    items_gained: ItemDeltaList = Field(default_factory=list)  # 本轮玩家获得的物品 → 确定性入库
    items_lost: ItemDeltaList = Field(default_factory=list)     # 本轮确定性失去/消耗/损毁的物品
    direction: DirectionPolicy = Field(default_factory=DirectionPolicy)
    # 本轮裁定涉及拿不准的具体规则时，planner 显式点名要查的规则关键词（如「霰弹枪 抵近 伤害」）。
    # 系统据此检索规则书原文喂给 KP——把「主动查规则」的判断交给稳定的裁定器，
    # 而非指望 KP 在叙事时自发喊 [RULE_LOOKUP]。空串 = 本轮无需专门查。
    rule_query: str = ""
    # 玩家本轮在**明确申请**某个技能检定（「我要投侦查」「用心理学看看他」）时填技能名——
    # 系统据此直接走确定性检定裁定路径（不再单独跑一次意图分诊 LLM 调用，省一段串行延迟）。
    # 普通行动/说话、战斗攻击宣言（走 combat）一律留空。
    player_check_request: str = ""

    @model_validator(mode="before")
    @classmethod
    def _tolerate_wrong_shapes(cls, data):
        """把 LLM 写错形状的字段就地归一，保住整份计划不因次要字段格式错误被整体丢弃。

        - 嵌套子模型字段给了非 dict（一句话/标量）→ 换成 {}，走该子模型默认
          （子模型自身的 field_validator，如 direction 的 pacing 归一，仍会生效）；
        - turn_kind 给了枚举外的值 → 退到 mixed。
        字符串列表字段（speakers/candidate_clue_ids/narration_brief 等）写成 null/标量的情形，
        交由字段级的 StrList（BeforeValidator）就地归一，这里不再重复处理。
        识别不了的一律退默认，绝不抛错。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name in _SUBMODEL_FIELDS:
            val = data.get(name)
            # 放行 dict（来自 JSON）与子模型实例（来自直接构造）；只拦截标量/字符串/列表等错误形状
            if name in data and not isinstance(val, (dict, BaseModel)):
                data[name] = {}
            elif isinstance(val, dict):
                # 子模型形状对，但内部标量 str 字段可能被写成 dict/list → 就地容错（否则子模型
                # 校验失败照样连累整份计划被丢弃）。
                sub_cls = cls.model_fields[name].annotation
                if isinstance(sub_cls, type) and issubclass(sub_cls, BaseModel):
                    data[name] = _coerce_str_fields(sub_cls, dict(val))
        if data.get("turn_kind") not in _TURN_KINDS:
            data.pop("turn_kind", None)  # 交回默认 "mixed"
        # auto_outcome 是枚举式 str（none/success/failure）：非字符串内容无意义，直接退默认，
        # 别拼成非法枚举值（mode=after 的 _combat_owns_resolution 也会兜非法值→none）。
        if "auto_outcome" in data and not isinstance(data["auto_outcome"], str):
            data["auto_outcome"] = "none"
        # 通用容错：顶层所有「纯 str」自由文本字段（player_intent / auto_outcome_reason 等）被写成
        # dict/list/标量 → 就地转字符串，保住整份计划不因某字段形状错误被整体丢弃回退旧流程。
        return _coerce_str_fields(cls, data)

    @model_validator(mode="after")
    def _combat_owns_resolution(self):
        """结算优先级与互斥：结构化战斗自行结算攻防（开战轮不挂普通检定、不走自动结局）；
        自动结局（success/failure）与掷骰互斥——一旦裁定免检直接判定，就不再 requires_check。"""
        if self.auto_outcome not in ("none", "success", "failure"):
            self.auto_outcome = "none"
        if self.combat.should_start:
            self.requires_check = False
            self.auto_outcome = "none"
        if self.auto_outcome in ("success", "failure"):
            self.requires_check = False
        return self


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
    rule_excerpts: list[dict] | None = None,
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
    # 场景连通：当前场景可直达的邻居（模组建了 connections 图才非空）。
    # scene_change 的确定性校验以同一张图为准——planner 别裁定不连通的切换。
    from app.services import session_service  # 局部导入避免顶层循环依赖

    scene_neighbors = session_service.scene_neighbors(module, session.current_scene_id)
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
        "scene_neighbors": scene_neighbors,
        # 幕后真相：全局裁定依据（线索该不该给、NPC 反应、危险判断都以真相为锚）
        "truth": (getattr(module, "truth", "") or "").strip(),
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
            if str(n).strip()
            and not (isinstance(e, dict) and (e.get("card") or {}).get("id"))
            and world_memory.is_plausible_npc_name(str(n))
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
                "是否必须切入结构化战斗，"
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
                "【检定裁定准则——决定难度 / 奖惩骰 / 是否免检的核心，按顺序问自己】\n"
                "(1) 结果是否**既不确定、两种走向又都有戏**？若在当前虚构态势下某个结果已成定局，就别掷骰：\n"
                "  · 玩家的处置让成功没有悬念（极贴切的现实话术 / 恰当的道具或环境优势达成目的）→ "
                "requires_check=false、auto_outcome=success，auto_outcome_reason 一句话说清凭什么免检；\n"
                "  · 当前态势让这次尝试注定落空（刚弄出巨响、已被锁定却仍想潜行）→ "
                "requires_check=false、auto_outcome=failure，auto_outcome_reason 说清为何必败。\n"
                "(2) 若确需掷骰，用**虚构态势**调节难度与奖惩骰（映射到有界档位，别自由捏目标值）：\n"
                "  · 明显有利（充分准备 / 工具到位 / 对方已松动 / 角度极佳）→ check.bonus=1，或 difficulty 降档；\n"
                "  · 明显不利（负伤 / 黑暗 / 嘈杂 / 目标已警觉 / 时间紧迫 / 行踪刚暴露）→ check.penalty=1，"
                "或 difficulty 升到 hard / extreme；check.reason 一句话写清依据（供 KP 入戏解释，不照念）。\n"
                "两条原型：①玩家用手机弄出巨响、把『循声辨位』的怪引来后还想潜行——声音已暴露位置，"
                "应 penalty 或 difficulty=extreme，甚至 auto_outcome=failure；②玩家用切中 NPC 动机、"
                "有理有据的现实话术说服对方——应 bonus / 降档，足够精彩则 auto_outcome=success 免检。\n"
                "auto_outcome 只用于结果**真的没有悬念**时；只要还有翻盘余地就掷骰——好 RP 给奖励骰 / 降档，"
                "而非直接判成功，尤其高风险场景别让口才碾平一切。\n"
                "combat.should_start 只在玩家或 NPC 已明确发起会造成伤害的攻击、双方即刻进入敌对交锋时为 true；"
                "威胁、戒备、瞄准、谈判或尚未接敌时保持 false。开战时 enemies 必须列出本轮实际参战敌方的名字，"
                "优先使用 visible_npcs 中的原名，trigger 用一句话说明开战原因。结构化战斗会自行结算攻击，"
                "因此规划开战时不要再把本次攻击裁定为普通 dice_check。\n"
                "combat_damage.trigger 在**战斗中玩家用引擎没建模的非常规/范围攻击命中敌人**时为 true："
                "燃烧弹/汽油弹泼火、群体波及、环境伤害（爆炸/塌方砸中）等（普通单体武器攻击走战斗面板、"
                "不在此列）。命中已由先前的投掷/攻击检定判定，这里只列后果：targets 填被波及的敌人名"
                "（用战斗态里的名字）；已知投掷武器填 weapon（如「莫洛托夫鸡尾酒」，系统查表拿 2D6 烧）、"
                "否则填 formula 骰式；burning=是否点燃。后端会让玩家亲手掷这份伤害、应用到所有波及敌人，"
                "不靠 KP 叙述扣血。\n"
                "current_scene.events 若非空，列出的是**模组明文规定的机制点**（进入场景所见/"
                "特定行动触发的理智检定、技能检定、伤害）：情景命中时**必须**按其规定裁定，数值照抄——"
                "san_check 的 san_loss 规格直接作为 sanity 的 success_loss/failure_loss，damage 的骰式"
                "直接作为伤害；有 events 依据时绝不用下面的通用建议档另行估值，也绝不漏掉。\n"
                "场景机制点的 note/trigger 若明确写了‘全员/全体/所有调查员/每名角色’等群检范围，"
                "check.chars 必须填‘在场’（或‘全体’）；单人机制点保持为空。不要因为玩家是主角"
                "就把明文规定的全员检定缩成主角检定。\n"
                "sanity.trigger 在**本轮有角色目睹或得知会动摇心智的恐怖**时为 true："
                "尸体/血腥惨状/怪物/超自然异象/亵渎的神话真相等；仅世俗惊吓（普通打斗、坏消息、"
                "寻常尸体已见过）不触发。true 时给 source（恐怖源标识，如「墓室腐尸」，同一源只检一次）、"
                "success_loss/failure_loss（按冲击：尸体 0/1d3，血腥或怪物 1/1d6，强大神话生物 1d6/1d20），"
                "witnesses 缺省=在场全体。后端会据此确定性发理智检定，不靠 KP 记得。\n"
                "mishap.trigger 仅在**本轮玩家掷出大失败、且其所做动作本身有身体危险**时为 true："
                "踢/扑正在燃烧或爆裂之物、攀高/走不稳结构、持械或搏斗、玩火电毒、强行破障等——大失败令"
                "危险反噬自身。true 时给 hp_delta（负整数，轻度反噬 -1~-3 如灼烧/擦碰/割伤，重度 -4~-6 如"
                "跌落/大面积灼伤）、target（受伤者，缺省=本轮掷骰玩家）、reason（一句话缘由）。后端据此确定性"
                "扣 HP，不靠 KP 记得。**非身体危险的大失败不触发**（图书馆/话术/侦查等失败只是没结果或误导，"
                "不掉血）；非大失败一律 false。\n"
                "items_gained/items_lost：本轮玩家**确实**获得或失去/用掉/损毁的物品——后端据此"
                "确定性增减库存，不靠 KP 记账。每项给 name、qty（缺省 1）、who（获得/失去者角色名，"
                "缺省=本轮行动玩家）；获得时 kind 可选 consumable/gear/key/document。只记**已然发生**"
                "的（捡起、被给、用掉最后一根火柴、绳子被割断）；仅打算拿、还没到手的不记。\n"
                "scene_policy.scene_change：本轮玩家**确实移动并到达了别的场景**时，填目标场景的 id 或"
                "名字（只能取运行时资料里的 current_scene / 可见场景，解析不到就别填）——后端据此确定性"
                "把角色位置与大地图切过去，不靠 KP 记得。**仅讨论/打算/建议去某地（『我们该先去X』）"
                "绝不填**：那只是商量，人没动；留空表示本轮仍在原场景。"
                "scene_neighbors 非空时目标还须与当前场景**连通**（相邻可直达；更远的连通地点须玩家"
                "实际走过去、叙事途经）——与之不连通的场景绝不填，系统也会拒绝这样的切换。\n"
                "player_check_request：玩家本轮是否在**明确申请**做某个技能/属性检定"
                "（如「我要投侦查」「让我用心理学看看他说的是真是假」）？是 → 填技能名"
                "（没点名技能就按其意图选最贴切的）；只是普通说话/行动/移动，或战斗攻击宣言"
                "（那走 combat.should_start），一律留空 \"\"。\n"
                "rule_query：当本轮裁定涉及你**没有十足把握的具体规则**时（特殊检定的精确用法、"
                "武器/法术数值、战斗细则、状态效果、疯狂症状表等），填一句要查的规则关键词"
                "（如「霰弹枪 抵近 伤害」「潜行 对抗 侦查」）——系统会据此检索规则书原文供裁定与叙事；"
                "对裁定有把握、或纯角色扮演回合则留空 \"\"。拿不准就填，宁可查一次也别凭印象编数值。\n"
                "npc_policy.speakers 与 direction.nudge 里的 NPC **只能用 canonical_npcs 里的名字**；"
                "improvised_npcs 是 KP 此前临场添加的龙套——**绝不安排他们携带线索、透露情报或推动剧情**，"
                "最多作为氛围出现，追问时指回模组内容。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + director_block
                + _rule_block(rule_excerpts)
            ),
        },
    ]


def _rule_block(rule_excerpts: list[dict] | None) -> str:
    """把检索到的规则书片段拼成一段「裁定参考」——planner 定检定难度/奖惩骰/SAN 时优先遵照条文，
    而非凭印象。空则返回空串（不注入）。"""
    if not rule_excerpts:
        return ""
    passages = "\n---\n".join(
        (h.get("text") or "").strip() for h in rule_excerpts if (h.get("text") or "").strip()
    )
    if not passages:
        return ""
    return (
        "\n\n规则要点（据此裁定难度/检定/奖惩骰/SAN，优先遵照条文而非凭印象；这些是系统按本轮"
        "情境预取的规则书原文，非玩家可见）：\n" + passages
    )


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
    if check.chars:
        parts.append(f"chars={check.chars}")
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
        "auto_outcome": plan.auto_outcome,
        "auto_outcome_reason": plan.auto_outcome_reason,
        "check": plan.check.model_dump(),
        "clue_policy": plan.clue_policy.model_dump(),
        "npc_policy": plan.npc_policy.model_dump(),
        "scene_policy": plan.scene_policy.model_dump(),
        "combat": plan.combat.model_dump(),
        "combat_damage": plan.combat_damage.model_dump(),
        "narration_brief": plan.narration_brief,
        "safety": plan.safety.model_dump(),
        "sanity": plan.sanity.model_dump(),
        "items_gained": [it.model_dump() for it in plan.items_gained],
        "items_lost": [it.model_dump() for it in plan.items_lost],
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
            "（skill/difficulty/visibility/chars 一律照计划 check 字段，不得改动、不得省略、不得替换成别的技能或范围。）\n"
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

    # 自动结局：虚构态势已让结果确定（免检），KP 必须据裁定确定性地把结果演出来——
    # 尤其 failure：绝不能因玩家申请了动作就叙述成宽松的成功（这正是「弄出巨响仍潜行成功」的病根）。
    auto_block = ""
    if plan.auto_outcome in ("success", "failure"):
        verdict = "直接成功" if plan.auto_outcome == "success" else "直接失败"
        reason = plan.auto_outcome_reason.strip() or (
            "当前情境已让结果没有悬念" if plan.auto_outcome == "success" else "当前情境已注定这次尝试落空"
        )
        auto_block = (
            "\n\n【自动结局——最高优先级裁定，凌驾叙事惯性】\n"
            f"本轮无需检定：玩家这次尝试**{verdict}**。入戏缘由：{reason}\n"
            "请据此**确定性地**叙述其结果，不要发起任何检定、不要含糊带过、不要给出与该裁定相反的走向：\n"
            + (
                "- 直接成功：让这次尝试顺遂达成，把玩家出色的临场处置在叙事里兑现成实打实的进展。\n"
                if plan.auto_outcome == "success" else
                "- 直接失败：让这次尝试当场落空并承担相应后果（被发现、被识破、错失时机等），"
                "**绝不能**因为玩家申请了这个动作就把它写成侥幸成功或悬而未决。\n"
            )
        )

    combat_block = ""
    if plan.combat.should_start:
        enemies = "、".join(plan.combat.enemies) or "（必须填写实际敌方名字）"
        combat_block = (
            "\n\n【结构化战斗切换——最高优先级状态约束】\n"
            "本轮裁定已经确认进入实战。你可以简短描写冲突爆发，但不得在自由叙事中自行判定命中、"
            "伤害或胜负；必须调用 start_combat，并在调用后立即收束本轮。\n"
            f"敌方：{enemies}\n"
            f"触发原因：{plan.combat.trigger or plan.player_intent}\n"
            "即使叙事已经写得完整，也不能省略战斗状态切换；后端会对漏调进行确定性补偿。\n"
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
            + auto_block
            + combat_block
        ),
    }
