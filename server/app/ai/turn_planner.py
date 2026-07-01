from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.models import Character, GameSession, Module

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
    玩家尚未到达区域的线索。
    """
    visible_ids = _visible_scene_ids(session)
    visible_clues = _filter_visible_items(module.clues, visible_ids)
    visible_npcs = _filter_visible_items(module.npcs, visible_ids)
    current_scene = next(
        (scene for scene in (module.scenes or []) if scene.get("id") == session.current_scene_id),
        None,
    )
    teammates = teammates or []
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
                "discovered": clue.get("discovered", False),
            }
            for clue in visible_clues
        ],
    }

    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 的 KP 回合规划器。你的任务不是写叙事，而是先判断本轮裁定："
                "玩家意图、是否需要检定、可揭示线索、NPC 反应、场景变化与安全边界。"
                "只输出一个 JSON object，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请基于以下运行时资料生成本轮裁定计划。"
                "线索只有在玩家行动匹配 trigger_condition 时才可进入 candidate_clue_ids。"
                "不得使用 visible_clues 以外的线索。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        },
    ]


async def run_turn_planner(llm: Any, messages: list[dict]) -> TurnPlan | None:
    try:
        raw = await llm.complete(
            messages,
            temperature=0,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        return TurnPlan.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        logger.warning("KP 回合规划器输出无法解析，已回退旧流程：%s", exc)
        return None
    except Exception:
        logger.exception("KP 回合规划器调用失败，已回退旧流程")
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
    }
    return {
        "role": "system",
        "content": (
            "【本轮裁定计划】\n"
            "你必须按此计划生成叙事和内部指令。隐藏信息只用于约束，不能写给玩家。"
            "若 requires_check 为 true，只描述尝试过程，并以计划指定的检定指令收尾。\n"
            + json.dumps(content, ensure_ascii=False, indent=2)
        ),
    }
