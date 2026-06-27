from __future__ import annotations

import json

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT, KP_OPENING_PROMPT
from app.ai.prompts.npc_system import NPC_SYSTEM_PROMPT
from app.ai.prompts.team_system import TEAM_SYSTEM_PROMPT

CONTEXT_TOKEN_BUDGET = 24000
RESERVE_FOR_OUTPUT = 4096
MAX_SYSTEM_TOKENS = 6000
MAX_SUMMARY_TOKENS = 1500
MIN_RECENT_EVENTS = 10
MAX_RECENT_EVENTS = 60


def _estimate_tokens(text: str) -> int:
    """粗估 token 数：中文约 1.5 token/字，英文约 0.75 token/word"""
    cn_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cn_chars
    return int(cn_chars * 1.5 + other * 0.4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """将文本截断到大约 max_tokens 以内"""
    if _estimate_tokens(text) <= max_tokens:
        return text
    ratio = max_tokens / max(_estimate_tokens(text), 1)
    cut = int(len(text) * ratio * 0.9)
    return text[:cut] + "\n…（内容过长，已截断）"


def _format_json(data) -> str:
    if not data:
        return "无"
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _format_json_compact(data) -> str:
    if not data:
        return "无"
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


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


def _compact_scenes(scenes: list[dict] | None, current_scene_id: str | None) -> str:
    """只保留当前和相邻场景的完整信息，其余只保留 id + name"""
    if not scenes:
        return "无"
    result = []
    for s in scenes:
        sid = s.get("id", "")
        if sid == current_scene_id:
            result.append(s)
        else:
            result.append({"id": sid, "name": s.get("name", ""), "description": s.get("description", "")[:60]})
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _compact_npcs(npcs: list[dict] | None) -> str:
    if not npcs:
        return "无"
    result = []
    for n in npcs:
        result.append({
            "id": n.get("id", ""),
            "name": n.get("name", ""),
            "description": (n.get("description") or "")[:100],
            "personality": (n.get("personality") or "")[:60],
            "secrets": (n.get("secrets") or "")[:100],
        })
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _compact_clues(clues: list[dict] | None) -> str:
    if not clues:
        return "无"
    result = []
    for c in clues:
        result.append({
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "description": (c.get("description") or "")[:80],
            "location": c.get("location", ""),
        })
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _format_player_info(char: Character) -> str:
    lines = [
        f"姓名：{char.name}",
        f"属性：{_format_json_compact(char.base_attributes)}",
        f"技能（非默认值）：{_format_json_compact({k: v for k, v in (char.skills or {}).items() if v > 0})}",
    ]
    sd = char.system_data or {}
    if sd.get("occupation"):
        lines.append(f"职业：{sd['occupation']}")
    if sd.get("age"):
        lines.append(f"年龄：{sd['age']}")
    if sd.get("gender"):
        lines.append(f"性别：{sd['gender']}")
    if sd.get("residence"):
        lines.append(f"住地：{sd['residence']}")
    if sd.get("birthplace"):
        lines.append(f"故乡：{sd['birthplace']}")
    hp = sd.get("hitPoints", {})
    san = sd.get("sanity", {})
    mp = sd.get("magicPoints", {})
    if hp:
        lines.append(f"HP：{hp.get('current', '?')}/{hp.get('max', '?')}")
    if san:
        lines.append(f"SAN：{san.get('current', '?')}/{san.get('max', '?')}")
    if mp:
        lines.append(f"MP：{mp.get('current', '?')}/{mp.get('max', '?')}")
    if char.backstory:
        lines.append(f"背景：{char.backstory[:200]}")
    return "\n".join(lines)


def _events_to_messages(
    events: list[EventLog],
    primary_char_id: str | None = None,
    party_char_ids: set[str] | None = None,
) -> list[dict]:
    """把事件流转成对话消息。

    ``party_char_ids`` 是玩家方（主角 + AI 队友）的角色 id 集合：他们的发言/行动
    都算 user 侧输入，**不会被误判成 KP 自己的 assistant 输出**。非主角的队友消息
    带上「队友·名字」前缀，让 KP 能区分谁在场、谁做了什么。
    """
    party = set(party_char_ids or ())
    if primary_char_id:
        party.add(primary_char_id)

    raw: list[dict] = []
    for ev in events:
        if ev.event_type in ("system", "ooc"):
            continue
        if ev.event_type in ("narration", "dice"):
            raw.append({"role": "assistant", "content": ev.content})
        elif ev.event_type == "dialogue":
            if ev.actor_id and ev.actor_id in party:
                if ev.actor_id == primary_char_id:
                    raw.append({"role": "user", "content": ev.content})
                else:
                    raw.append({
                        "role": "user",
                        "content": f"[队友·{ev.actor_name}] “{ev.content}”",
                    })
            elif ev.actor_name:
                raw.append({"role": "assistant", "content": ev.actor_name + "：“" + ev.content + "”"})
            else:
                raw.append({"role": "user", "content": ev.content})
        elif ev.event_type == "action":
            if ev.actor_id and ev.actor_id in party and ev.actor_id != primary_char_id:
                raw.append({"role": "user", "content": f"[队友·{ev.actor_name} 行动] " + ev.content})
            else:
                raw.append({"role": "user", "content": "[行动] " + ev.content})
    merged: list[dict] = []
    for msg in raw:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1] = {
                **merged[-1],
                "content": merged[-1]["content"] + "\n" + msg["content"],
            }
        else:
            merged.append(msg)
    return merged


def _format_teammate_brief(char: Character) -> str:
    """队友的精简画像：姓名 + 职业 + 关键技能 + 一句背景。"""
    sd = char.system_data or {}
    parts = [f"- {char.name}"]
    if sd.get("occupation"):
        parts.append(f"（{sd['occupation']}）")
    top_skills = sorted(
        ((k, v) for k, v in (char.skills or {}).items() if v >= 50),
        key=lambda kv: kv[1],
        reverse=True,
    )[:4]
    if top_skills:
        parts.append("，擅长：" + "、".join(f"{k}{v}" for k, v in top_skills))
    line = "".join(parts)
    if char.backstory:
        line += f"。背景：{char.backstory[:80]}"
    return line


def build_kp_context(
    session: GameSession,
    module: Module,
    player_char: Character,
    events: list[EventLog],
    teammates: list[Character] | None = None,
) -> list[dict]:
    current_scene = _find_scene(module, session.current_scene_id)

    teammates = teammates or []
    player_info = _format_player_info(player_char)
    if teammates:
        team_lines = "\n".join(_format_teammate_brief(t) for t in teammates)
        player_info += (
            f"\n\n## 同场的其他玩家角色（共 {len(teammates)} 名，与上面这位**地位完全平等**）\n"
            "本场是多人同桌：以下每个都是独立的玩家方角色（由真人或各自的 AI 扮演），"
            "他们会自行说话和行动，发言作为独立消息出现（形如「[队友·名字] …」）。\n"
            + team_lines
            + "\n\n**多人叙事铁律（违反即严重错误）**：\n"
            "1. **平等对待所有玩家角色**——开场白和叙事绝不要只围绕某一个人（尤其别独宠主角），"
            "要让每位在场角色都有存在感、各自登场；点名、给戏份要照顾到所有人。\n"
            "2. **绝不替任何玩家角色行动或说话**——包括上面这位主角和这些同伴："
            "不写他们的台词、不描述他们的主动行动/姿态/心理活动、不替他们做决定。"
            "他们做什么、说什么，全部由他们自己产出，不归你管。\n"
            "你只负责：描述环境与场景、扮演模组 NPC、裁定检定、对全队已经做出的行动给出世界的回应。"
        )

    system_content = KP_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        module_title=module.title,
        module_description=module.description,
        world_setting=_format_json_compact(module.world_setting),
        scenes_info=_compact_scenes(module.scenes, session.current_scene_id),
        current_scene=_format_json(current_scene) if current_scene else "初始场景",
        npcs_info=_compact_npcs(module.npcs),
        clues_info=_compact_clues(module.clues),
        player_info=player_info,
    )

    party_char_ids = {player_char.id} | {t.id for t in teammates}

    system_tokens = _estimate_tokens(system_content)
    if system_tokens > MAX_SYSTEM_TOKENS:
        system_content = _truncate_to_tokens(system_content, MAX_SYSTEM_TOKENS)
        system_tokens = MAX_SYSTEM_TOKENS

    messages = [{"role": "system", "content": system_content}]

    if not events:
        opening = KP_OPENING_PROMPT
        if teammates:
            opening += (
                f"\n\n【多人开场】在场共 {len(teammates) + 1} 名玩家角色，地位平等。"
                "开场白要让每一位都自然登场、各有存在感，不要只对某一个人说话、也不要把镜头只对准主角。"
                "只铺好场景、氛围与全队共同面对的处境即可；"
                "绝不要替任何玩家描写其动作、姿态或台词——把第一步行动权完整留给玩家们。"
            )
        messages.append({"role": "user", "content": opening})
    else:
        event_budget = CONTEXT_TOKEN_BUDGET - system_tokens - RESERVE_FOR_OUTPUT

        all_msgs = _events_to_messages(
            events, primary_char_id=player_char.id, party_char_ids=party_char_ids,
        )

        if len(all_msgs) <= MIN_RECENT_EVENTS:
            recent_msgs = all_msgs
            summary = ""
        else:
            recent_msgs = all_msgs[-MIN_RECENT_EVENTS:]
            recent_tokens = sum(_estimate_tokens(m["content"]) for m in recent_msgs)

            remaining = event_budget - recent_tokens
            while remaining > 0 and len(recent_msgs) < len(all_msgs):
                next_idx = len(all_msgs) - len(recent_msgs) - 1
                next_msg = all_msgs[next_idx]
                msg_tokens = _estimate_tokens(next_msg["content"])
                if remaining - msg_tokens < 0 and len(recent_msgs) >= MIN_RECENT_EVENTS:
                    break
                recent_msgs.insert(0, next_msg)
                remaining -= msg_tokens

            if len(recent_msgs) < len(all_msgs):
                older_events = events[:len(events) - len(recent_msgs)]
                summary = _summarize_old_events(older_events, max_tokens=min(MAX_SUMMARY_TOKENS, max(remaining, 500)))
            else:
                summary = ""

        if summary:
            messages.append({
                "role": "system",
                "content": "[之前发生的事件摘要]\n" + summary,
            })
        messages.extend(recent_msgs)
        if len(messages) >= 3 and messages[-1]["role"] == "user":
            messages.insert(-1, {
                "role": "system",
                "content": (
                    "[格式提醒] NPC说话时必须写出台词原文，"
                    "用中文双引号（“”）包裹。"
                    "不要只描述NPC的声音、语气或动作而省略实际台词内容。"
                ),
            })

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
    player_cid = session.player_character_id
    messages.extend(_events_to_messages(visible_events[-20:], primary_char_id=player_cid))

    if trigger_context:
        messages.append({
            "role": "user",
            "content": "[场景] " + trigger_context + "\n请以你的角色身份回应。",
        })

    return messages


def build_team_context(
    teammate: Character,
    session: GameSession,
    module: Module,
    events: list[EventLog],
    player_char: Character,
    all_teammates: list[Character] | None = None,
) -> list[dict]:
    """构建单个 AI 队友的决策上下文：场景 + 队伍 + 最近事件。"""
    current_scene = _find_scene(module, session.current_scene_id)

    party_members = [player_char] + [
        t for t in (all_teammates or []) if t.id != teammate.id
    ]
    party_info = "\n".join(
        f"- 主角：{player_char.name}"
        if m.id == player_char.id
        else f"- 队友：{m.name}"
        for m in party_members
    ) or "无"

    system_content = TEAM_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        name=teammate.name,
        char_info=_format_player_info(teammate),
        scene=_format_json(current_scene) if current_scene else "初始场景",
        party_info=party_info,
    )

    digest = _format_recent_events_digest(
        events[-20:], primary_char_id=player_char.id, self_char_id=teammate.id,
    )

    messages = [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": (
                "## 最近发生的事（最新在最后）\n"
                + digest
                + "\n\n轮到你了。请根据主角刚才的行动和当前局面，"
                "决定你这一回合做什么，并按 JSON 格式输出。"
            ),
        },
    ]
    return messages


def _format_recent_events_digest(
    events: list[EventLog],
    primary_char_id: str | None = None,
    self_char_id: str | None = None,
) -> str:
    """把最近事件渲染成给 AI 队友看的纯文本摘要（不做角色身份映射）。"""
    lines: list[str] = []
    for ev in events:
        content = (ev.content or "").strip()
        if not content:
            continue
        if ev.event_type == "narration":
            lines.append(f"- 旁白：{content}")
        elif ev.event_type == "dice":
            lines.append(f"- 检定：{content}")
        elif ev.event_type == "system":
            lines.append(f"- 系统：{content}")
        elif ev.event_type in ("dialogue", "action"):
            if ev.actor_id == self_char_id:
                who = "你"
            elif ev.actor_id == primary_char_id:
                who = f"主角{('·' + ev.actor_name) if ev.actor_name else ''}"
            else:
                who = ev.actor_name or "某人"
            verb = "说" if ev.event_type == "dialogue" else "行动"
            lines.append(f"- {who}{verb}：{content}")
    return "\n".join(lines) if lines else "（暂无）"


def _summarize_old_events(events: list[EventLog], max_tokens: int = 1500) -> str:
    if not events:
        return ""
    for ev in reversed(events):
        if ev.summary:
            return _truncate_to_tokens(ev.summary, max_tokens)

    lines = []
    token_count = 0
    for ev in events:
        prefix = ev.actor_name or ev.event_type
        snippet = ev.content[:120].replace("\n", " ")
        line = f"- [{prefix}] {snippet}"
        line_tokens = _estimate_tokens(line)
        if token_count + line_tokens > max_tokens:
            break
        lines.append(line)
        token_count += line_tokens

    return "\n".join(lines) if lines else ""
