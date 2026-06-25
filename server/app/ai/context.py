from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT, KP_OPENING_PROMPT
from app.ai.prompts.npc_system import NPC_SYSTEM_PROMPT

MAX_RECENT_EVENTS = 40
SUMMARY_THRESHOLD = 60


def _format_json(data) -> str:
    if not data:
        return "无"
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _find_npc_def(module: Module, npc_id: str) -> dict | None:
    for npc in (module.npcs or []):
        if npc.get("id") == npc_id:
            return npc
    return None


def _find_scene(module: Module, scene_id: str | None) -> dict | None:
    if not scene_id:
        return (module.scenes or [{}])[0] if module.scenes else None
    for s in (module.scenes or []):
        if s.get("id") == scene_id:
            return s
    return None


def _format_player_info(char: Character) -> str:
    lines = [
        f"姓名：{char.name}",
        f"属性：{_format_json(char.base_attributes)}",
        f"技能（非默认值）：{_format_json({k: v for k, v in (char.skills or {}).items() if v > 0})}",
    ]
    sd = char.system_data or {}
    hp = sd.get("hitPoints", {})
    san = sd.get("sanity", {})
    if hp:
        lines.append(f"HP：{hp.get('current', '?')}/{hp.get('max', '?')}")
    if san:
        lines.append(f"SAN：{san.get('current', '?')}/{san.get('max', '?')}")
    if char.backstory:
        lines.append(f"背景：{char.backstory}")
    return "\n".join(lines)


def _events_to_messages(events: list[EventLog]) -> list[dict]:
    messages = []
    for ev in events:
        if ev.event_type == "system":
            continue
        if ev.event_type in ("narration", "dice"):
            messages.append({"role": "assistant", "content": ev.content})
        elif ev.event_type == "dialogue":
            if ev.actor_name and ev.actor_name != "玩家":
                messages.append({
                    "role": "assistant",
                    "content": f"[{ev.actor_name}] {ev.content}",
                })
            else:
                messages.append({"role": "user", "content": ev.content})
        elif ev.event_type == "action":
            messages.append({"role": "user", "content": f"[行动] {ev.content}"})
    return messages


def build_kp_context(
    session: GameSession,
    module: Module,
    player_char: Character,
    events: list[EventLog],
) -> list[dict]:
    current_scene = _find_scene(module, session.current_scene_id)

    system_content = KP_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        module_title=module.title,
        module_description=module.description,
        world_setting=_format_json(module.world_setting),
        scenes_info=_format_json(module.scenes),
        current_scene=_format_json(current_scene) if current_scene else "初始场景",
        npcs_info=_format_json(module.npcs),
        clues_info=_format_json(module.clues),
        player_info=_format_player_info(player_char),
    )

    messages = [{"role": "system", "content": system_content}]

    if not events:
        messages.append({"role": "user", "content": KP_OPENING_PROMPT})
    else:
        recent = events[-MAX_RECENT_EVENTS:]
        if len(events) > SUMMARY_THRESHOLD:
            older_summary = _summarize_old_events(events[:-MAX_RECENT_EVENTS])
            if older_summary:
                messages.append({
                    "role": "system",
                    "content": f"[之前发生的事件摘要]\n{older_summary}",
                })
        messages.extend(_events_to_messages(recent))

    return messages


def build_npc_context(
    npc_id: str,
    session: GameSession,
    module: Module,
    events: list[EventLog],
    trigger_context: str = "",
) -> list[dict]:
    npc_def = _find_npc_def(module, npc_id)
    if not npc_def:
        npc_def = {"name": "未知NPC", "description": "", "personality": "", "secrets": ""}

    system_content = NPC_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        npc_name=npc_def.get("name", "未知"),
        npc_description=npc_def.get("description", ""),
        npc_personality=npc_def.get("personality", "普通人"),
        npc_secrets=npc_def.get("secrets", "无"),
    )

    visible_events = [
        ev for ev in events
        if not ev.visibility or npc_id in ev.visibility
    ]

    messages = [{"role": "system", "content": system_content}]
    messages.extend(_events_to_messages(visible_events[-20:]))

    if trigger_context:
        messages.append({
            "role": "user",
            "content": f"[场景] {trigger_context}\n请以你的角色身份回应。",
        })

    return messages


def _summarize_old_events(events: list[EventLog]) -> str:
    if not events:
        return ""
    for ev in reversed(events):
        if ev.summary:
            return ev.summary
    lines = []
    for ev in events[-10:]:
        prefix = ev.actor_name or ev.event_type
        lines.append(f"- [{prefix}] {ev.content[:80]}")
    return "\n".join(lines)
