from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

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
    """导演层：本轮的节奏经营意图。只影响「怎么讲」，不改变世界状态。"""

    pacing: Literal["hold", "tighten", "release"] = "hold"
    spotlight: list[str] = Field(default_factory=list)  # 本轮应主动给戏份的角色名
    nudge: str = ""  # 卡关时的推进手段（让线索更显眼/NPC 主动接触），不得直接判定检定成功
    foreshadow: str = ""  # 建议埋设或回收的悬念，一句话


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
    resolved_npcs = [_resolve_state(npc, flags) for npc in (module.npcs or [])]

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
                "以及导演层的节奏经营（direction：pacing/spotlight/nudge/foreshadow）。"
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
                "status=partial 的仅在玩家行动继续深入时可作为升级揭示的 candidate。\n"
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
    return {
        "role": "system",
        "content": (
            "【本轮裁定计划】（内部工作稿，仅供你裁定参考——不是要念给玩家听的内容）\n"
            "你必须据此计划生成本回合叙事和内部指令，但绝不能把下面 JSON 的字段名、结构、"
            "或 flag/线索/NPC 的内部 id 等技术性标识，以任何形式（复述、总结、列表、标题）"
            "写进给玩家看的文本；看到这份结构化计划**不代表要改用「汇报体」输出**——回复必须"
            "仍是紧贴情境的自然语言叙事，不得另起标题分段或项目符号列表汇报状态。\n"
            "若 requires_check 为 true，只描述尝试过程，并以计划指定的检定指令收尾。"
            "safety.do_not_reveal 的内容不能通过任何暗示性总结泄露。\n"
            "direction 是导演笔记（内部指引，严禁向玩家复述原文）：pacing 是本轮节奏"
            "（tighten=收紧推进/release=放松换气/hold=保持）；spotlight 列出的角色本轮要"
            "自然地给到戏份或点名；nudge 是解卡手段，只能让线索更显眼或让 NPC 主动接触，"
            "绝不能替玩家决定或直接宣布检定成功；foreshadow 是可择机埋设/回收的悬念。\n"
            + json.dumps(content, ensure_ascii=False, indent=2)
        ),
    }
