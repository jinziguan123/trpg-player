from __future__ import annotations

import json
import logging

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.ai.prompts.kp_system import (
    KP_SYSTEM_PROMPT,
    KP_OPENING_PROMPT,
    HANDOUT_INSTRUCTION,
    MODULE_EXCERPT_SECTION,
    MODULE_LOOKUP_INSTRUCTION,
    MOVE_INSTRUCTION,
    GROUP_INSTRUCTION,
    RULE_LOOKUP_INSTRUCTION,
    PLOT_FLAG_INSTRUCTION,
)
from app.ai.prompts.npc_system import NPC_SYSTEM_PROMPT
from app.ai.prompts.team_system import (
    TEAM_MODE_SEPARATED,
    TEAM_MODE_TOGETHER,
    TEAM_SYSTEM_PROMPT,
)
from app.services import world_memory

logger = logging.getLogger(__name__)

# 上下文总预算（输入 + 输出，按 _estimate_tokens 粗估）。现代模型上下文窗口普遍 ≥64K
# （DeepSeek 64K、Claude 200K），早先 24000 是保守值；提高到 40000 让长会话保留更多事件史，
# 仍对 64K 窗口留足 tokenizer 估算误差的安全余量。
CONTEXT_TOKEN_BUDGET = 40000
# 输出预留：KP 叙事（尤其分头/多 NPC/tool-loop 多步）可能较长，给足避免被截断。
RESERVE_FOR_OUTPUT = 6000
# 系统提示上限。此处装的不只是静态裁定手册，还有模组数据 + RAG 原文摘录（可达 ~2700 token）
# + 线索台账 + NPC 记忆 + 幕后动态 + handout 清单（P1-P3 陆续加入）。6000 已装不下这些叠加、
# 会截掉真实内容 → 提升到 12000，让系统提示完整呈现，直接改善 KP 裁定质量。
MAX_SYSTEM_TOKENS = 12000
MAX_SUMMARY_TOKENS = 2000
MIN_RECENT_EVENTS = 10
MAX_RECENT_EVENTS = 60
# 被动注入的模组原文摘录：单块截断（摘录段计入 MAX_SYSTEM_TOKENS 预算）。系统预算放宽后
# 可给更完整的原文片段，从 400 提到 600 字。
MODULE_EXCERPT_MAX_CHARS = 600
# 「幕后动态」小节最多注入最近几条幕后事件（visibility=["kp"]，仅 KP 可见）
MAX_BACKSTAGE_IN_CONTEXT = 5


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


def _active_flags(session: GameSession) -> set[str]:
    """当前已激活的剧情标志集合（world_state.flags 容忍 dict / list 两种形态）。"""
    raw = (session.world_state or {}).get("flags")
    if isinstance(raw, dict):
        return {k for k, v in raw.items() if v}
    if isinstance(raw, (list, set, tuple)):
        return set(raw)
    return set()


def _resolve_state(entity: dict, flags: set[str]) -> dict:
    """按已激活 flags 把 entity.states 里命中的变体依次覆盖到基础字段上（后命中者优先）。

    变体形如 ``{"when": ["flag_a", ...], <要覆盖的字段>}``：when 内的 flag 全部已激活才生效，
    缺省/空 when 视为恒命中。无 states 或无任何命中时返回基础字段本身（向后兼容）。
    """
    states = entity.get("states")
    if not states:
        return entity
    base = {k: v for k, v in entity.items() if k != "states"}
    for st in states:
        when = st.get("when") or []
        if isinstance(when, str):
            when = [when]
        if all(w in flags for w in when):
            base.update({k: v for k, v in st.items() if k != "when"})
    return base


def _resolve_scene(scenes: list[dict] | None, scene_id: str | None, flags: set[str]) -> dict | None:
    """在已按 flags 解析过状态的场景列表里查当前场景。"""
    scenes = scenes or []
    if not scene_id:
        return scenes[0] if scenes else None
    for s in scenes:
        if s.get("id") == scene_id:
            return s
    return None


def _format_triggers(triggers: list[dict] | None) -> str:
    """把作者设定的剧情触发器渲染成「当 X → 置/清某标志」的指引，供 KP 判断何时发标签。"""
    if not triggers:
        return ""
    lines: list[str] = []
    for t in triggers:
        if not isinstance(t, dict):
            continue
        when = str(t.get("when") or t.get("condition") or "").strip()
        sets = t.get("set_flags") or t.get("flags") or []
        clears = t.get("clear_flags") or []
        if isinstance(sets, str):
            sets = [sets]
        if isinstance(clears, str):
            clears = [clears]
        if not when or not (sets or clears):
            continue
        parts = []
        if sets:
            parts.append("置 " + "、".join(sets))
        if clears:
            parts.append("清 " + "、".join(clears))
        lines.append(f"- 当{when} → {'，'.join(parts)}")
    return "\n".join(lines)


def _format_plot_state(flags: set[str], triggers: list[dict] | None = None) -> str:
    lines: list[str] = []
    if flags:
        lines.append(
            "已激活标志：" + "、".join(sorted(flags))
            + "。场景与 NPC 的当前样貌已据此切换——叙述务必贴合当前状态，不要退回到旧样貌。"
        )
    else:
        lines.append("（暂无特殊剧情标志，各场景/NPC 按其默认状态叙述）")
    guide = _format_triggers(triggers)
    if guide:
        lines.append("剧情推进指引（达成对应条件时按下文规则发 [SET_FLAG]/[CLEAR_FLAG]）：\n" + guide)
    return "\n".join(lines)


def _has_plot_state(module: Module) -> bool:
    """模组是否定义了「随剧情改变」的内容（带 states 的场景/NPC，或 triggers）。"""
    if module.triggers:
        return True
    if any(s.get("states") for s in (module.scenes or [])):
        return True
    if any(n.get("states") for n in (module.npcs or [])):
        return True
    return False


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


def _compact_npcs(
    npcs: list[dict] | None,
    only_scene_id: str | None = None,
    hide_secrets: bool = False,
    visible_scene_ids: set[str] | None = None,
) -> str:
    """压缩 NPC 列表。

    ``only_scene_id`` 给定时（开场用）只保留 initial_location 命中起始场景的 NPC，
    把藏在深处的 NPC（如墓室里的尸体）挡在开场之外；``hide_secrets`` 剥掉 secrets。

    ``visible_scene_ids`` 给定时（运行时分层用）只保留 initial_location ∈ 已访问场景、
    或没有固定位置的 NPC，避免把玩家尚未到达区域的 NPC 提前喂给 KP。
    """
    if not npcs:
        return "无"
    result = []
    for n in npcs:
        loc = n.get("initial_location")
        if only_scene_id is not None and loc != only_scene_id:
            continue
        if visible_scene_ids is not None and loc and loc not in visible_scene_ids:
            continue
        item = {
            "id": n.get("id", ""),
            "name": n.get("name", ""),
            "description": (n.get("description") or "")[:100],
            "personality": (n.get("personality") or "")[:60],
        }
        if n.get("attributes"):  # CoC 九维：非剧透，供对抗/战斗判断与叙述参考
            item["attributes"] = n.get("attributes")
        if n.get("alive") is False:  # 剧情变体可将 NPC 标记为已死亡
            item["status"] = "已死亡（不可再开口或行动）"
        if not hide_secrets:
            # 生平含 KP 视角的过往，可能涉剧情 → 与 secrets 同样仅运行时给、开场剥离
            if n.get("background"):
                item["background"] = (n.get("background") or "")[:120]
            item["secrets"] = (n.get("secrets") or "")[:100]
        result.append(item)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result else "无"


def _compact_clues(
    clues: list[dict] | None,
    visible_scene_ids: set[str] | None = None,
) -> str:
    """压缩线索列表。

    ``visible_scene_ids`` 给定时（运行时分层用）只保留 location ∈ 已访问场景、或没有
    绑定场景的线索，避免把玩家尚未到达区域的线索提前喂给 KP（防中途泄露）。
    """
    if not clues:
        return "无"
    result = []
    for c in clues:
        loc = c.get("location")
        if visible_scene_ids is not None and loc and loc not in visible_scene_ids:
            continue
        result.append({
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "description": (c.get("description") or "")[:80],
            "location": c.get("location", ""),
        })
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result else "无"


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

    ``party_char_ids`` 是玩家方全部角色（含房主角色与 AI 队友）的 id 集合：他们的发言/
    行动都算 user 侧输入，**不会被误判成 KP 自己的 assistant 输出**。所有玩家角色**一视同仁**，
    统一以「[名字]」标注是谁在说/做，无主角特权。``primary_char_id`` 仅用于并入 party。
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
                name = ev.actor_name or "队员"
                raw.append({"role": "user", "content": f"[{name}] “{ev.content}”"})
            elif ev.actor_name:
                raw.append({"role": "assistant", "content": ev.actor_name + "：“" + ev.content + "”"})
            else:
                raw.append({"role": "user", "content": ev.content})
        elif ev.event_type == "action":
            if ev.actor_id and ev.actor_id in party:
                name = ev.actor_name or "队员"
                raw.append({"role": "user", "content": f"[{name} 行动] " + ev.content})
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


def _format_party_member(char: Character) -> str:
    """玩家角色的统一精简画像（所有玩家角色一视同仁）：姓名 + 职业 + 状态 + 关键技能 + 一句背景。"""
    sd = char.system_data or {}
    parts = [f"- {char.name}"]
    if sd.get("occupation"):
        parts.append(f"（{sd['occupation']}）")
    hp = sd.get("hitPoints", {})
    san = sd.get("sanity", {})
    cond = []
    if hp:
        cond.append(f"HP{hp.get('current', '?')}/{hp.get('max', '?')}")
    if san:
        cond.append(f"SAN{san.get('current', '?')}/{san.get('max', '?')}")
    if cond:
        parts.append("｜" + " ".join(cond))
    top_skills = sorted(
        ((k, v) for k, v in (char.skills or {}).items() if v >= 50),
        key=lambda kv: kv[1],
        reverse=True,
    )[:4]
    if top_skills:
        parts.append("，擅长：" + "、".join(f"{k}{v}" for k, v in top_skills))
    line = "".join(parts)
    if char.backstory:
        line += f"。背景：{char.backstory[:70]}"
    return line


# 手书类型 → 中文标签（未知类型原样透传）
_HANDOUT_KIND_LABEL = {"letter": "信件", "news": "报纸", "diary": "日记", "note": "便条"}


def _format_handout_list(module: Module, session: GameSession) -> str:
    """尚未发放的手书清单行（id｜类型｜标题｜发放条件；**正文绝不进清单**——发放时才展开）。

    已发放的（world_state.handouts_issued）滤掉——它们经线索台账自然呈现；
    全部发完或模组无手书时返回空串（调用方不注入小节）。
    """
    issued = set((session.world_state or {}).get("handouts_issued") or [])
    lines: list[str] = []
    for h in (getattr(module, "handouts", None) or []):
        if not isinstance(h, dict):
            continue
        hid = str(h.get("id") or "").strip()
        if not hid or hid in issued:
            continue
        kind = str(h.get("kind") or "").strip()
        parts = [
            hid,
            _HANDOUT_KIND_LABEL.get(kind, kind or "文书"),
            str(h.get("title") or "").strip() or "（无题）",
            str(h.get("trigger_condition") or "").strip() or "（KP 视剧情裁量）",
        ]
        lines.append("- " + "｜".join(parts))
    return "\n".join(lines)


def _format_module_excerpts(excerpts: list[dict]) -> str:
    """把检索到的原文片段渲染成摘录小节正文：逐块截断 + 编号平铺。"""
    lines: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        text = (ex.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MODULE_EXCERPT_MAX_CHARS:
            text = text[:MODULE_EXCERPT_MAX_CHARS] + "…"
        lines.append(f"[摘录 {i}] {text}")
    return "\n".join(lines)


def _format_backstage_section(events: list[EventLog]) -> str:
    """KP 上下文的「幕后动态」小节：最近几条幕后推演事件（metadata.kind=backstage）。

    这是「仅 KP 可见」事件的唯一注入出口——玩家侧（历史/搜索/队友/NPC/广播）全部
    过滤。无幕后事件返回空串（向后兼容，不注入）。
    """
    rows = [
        e for e in (events or [])
        if (e.metadata_ or {}).get("kind") == "backstage"
    ][-MAX_BACKSTAGE_IN_CONTEXT:]
    if not rows:
        return ""
    lines = [
        "【幕后动态】（玩家不可见——世界在他们视野之外的演进，仅供你把握局势，"
        "绝不直接复述给玩家）"
    ]
    for e in rows:
        meta = e.metadata_ or {}
        line = f"- {(e.content or '').strip()}"
        flags = [str(f) for f in (meta.get("suggest_flags") or []) if str(f).strip()]
        if flags:
            line += f"（若剧情落实，可考虑推进标志：{'、'.join(flags)}）"
        lines.append(line)
    lines.append(
        "以上幕后事件玩家尚未目睹：绝不在旁白中直接宣告、总结或暗示它们发生过；"
        "只有当玩家到场、调查或与相关 NPC 互动时，才让他们以「结果与痕迹」的方式自然发现。"
        "所列推进标志只是建议，是否 [SET_FLAG] 由你在叙事时机成熟时决定。"
    )
    return "\n".join(lines)


def build_kp_context(
    session: GameSession,
    module: Module,
    player_char: Character,
    events: list[EventLog],
    teammates: list[Character] | None = None,
    rules_lookup_enabled: bool = False,
    viewer_scene_id: str | None = None,
    module_excerpts: list[dict] | None = None,
    module_lookup_enabled: bool = False,
) -> list[dict]:
    # 本函数保持纯粹（不触数据库）：module_excerpts 是调用方（chat_service）检索好的
    # 模组原文片段（[{"text", ...}]），未建索引/检索失败时传 None → 行为与无此特性时完全一致。
    # 分头行动时，每个分组各以「该组所在场景」为锚构建上下文（current_scene / 可见 NPC / 线索 /
    # 场景清单），否则所有分组都拿到主角场景的资料，KP 只能把主角场景重复叙述一遍。
    # 默认 None → 回落主角所在场景（session.current_scene_id），非分头场景行为不变。
    scene_id = viewer_scene_id or session.current_scene_id
    # 剧情状态：按已激活 flags 把场景/NPC 解析到「当前样貌」，再喂给 KP（向后兼容：无 states 即原样）。
    flags = _active_flags(session)
    scenes = [_resolve_state(s, flags) for s in (module.scenes or [])]
    npcs = [_resolve_state(n, flags) for n in (module.npcs or [])]
    current_scene = _resolve_scene(scenes, scene_id, flags)

    teammates = teammates or []
    if teammates:
        # 多人同桌：全部玩家角色一视同仁地平铺成队伍名册，无主角特权。
        party = [player_char] + teammates
        roster = "\n".join(_format_party_member(m) for m in party)
        player_info = (
            f"本场是多人同桌，共 {len(party)} 名玩家角色，**地位完全平等**（由真人或各自的 AI 扮演）。"
            "他们各自说话和行动，发言作为独立消息出现（形如「[名字] …」）。队伍名册：\n"
            + roster
            + "\n\n**多人叙事铁律（违反即严重错误）**：\n"
            "1. **平等对待所有玩家角色**——开场白和叙事绝不要只围绕某一个人（没有"
            "「主角」一说，名册第一位只是房主、并不更重要），要让每位角色都有存在感、各自登场；"
            "点名、给戏份要照顾到所有人。\n"
            "2. **绝不替任何玩家角色行动或说话**——不写他们的台词、不描述他们的主动行动/姿态/"
            "心理活动、不替他们做决定。他们做什么、说什么全部由他们自己产出，不归你管。\n"
            "你只负责：描述环境与场景、扮演模组 NPC、裁定检定、对全队已做出的行动给出世界的回应。"
        )
    else:
        # 单人单角色：直接给完整角色卡（只有一名玩家角色，无平等性问题）。
        player_info = _format_player_info(player_char)

    # 开场隔离：开场（无历史事件）时只给起始场景的 NPC、剥掉 secrets，线索完全不给——
    # 防止 KP 拿"待发现"的尸体/笔记/线索现编进开场白。游戏开始后恢复完整资料。
    is_opening = not events
    if is_opening:
        npcs_info = _compact_npcs(
            npcs, only_scene_id=scene_id, hide_secrets=True,
        )
        clues_info = "（线索是 KP 专属资料，开场绝不涉及；只能在玩家实际调查发现时才出现）"
    else:
        # 运行时分层：只把玩家已到达区域的 NPC / 线索喂给 KP，减少中途泄露未抵达
        # 区域的内容。无固定位置的 NPC / 线索照常给。
        visible_scene_ids = set((session.world_state or {}).get("visited_scenes") or [])
        if scene_id:
            visible_scene_ids.add(scene_id)
        npcs_info = _compact_npcs(npcs, visible_scene_ids=visible_scene_ids)
        clues_info = _compact_clues(module.clues, visible_scene_ids=visible_scene_ids)

    system_content = KP_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        module_title=module.title,
        module_description=module.description,
        world_setting=_format_json_compact(module.world_setting),
        scenes_info=_compact_scenes(scenes, scene_id),
        current_scene=_format_json(current_scene) if current_scene else "初始场景",
        plot_state=_format_plot_state(flags, module.triggers),
        npcs_info=npcs_info,
        clues_info=clues_info,
        player_info=player_info,
    )

    # 模组原文摘录（被动注入）：调用方检索好才有，独立小节、带泄密警示措辞。
    if module_excerpts:
        excerpt_body = _format_module_excerpts(module_excerpts)
        if excerpt_body:
            system_content += MODULE_EXCERPT_SECTION.format(excerpts=excerpt_body)

    # 仅在挂载了规则书时广告 [RULE_LOOKUP] 能力（无书时不让 KP 发空查询）。
    if rules_lookup_enabled:
        system_content += RULE_LOOKUP_INSTRUCTION

    # 仅在模组原文索引就绪时广告 [MODULE_LOOKUP] 能力（未建索引不让 KP 发空查询）。
    if module_lookup_enabled:
        system_content += MODULE_LOOKUP_INSTRUCTION

    # 仅当模组确有「随剧情改变」的场景/NPC 时，且非开场，才广告 [SET_FLAG]/[CLEAR_FLAG] 推进能力。
    if not is_opening and _has_plot_state(module):
        system_content += PLOT_FLAG_INSTRUCTION

    # 仅当前场景有地图时，广告 [MOVE] 走位能力（让地图反映玩家/NPC 实际位置）。
    if current_scene and current_scene.get("map"):
        system_content += MOVE_INSTRUCTION

    # 队伍可能分头（有队友）时，广告 [GROUP] 分组标记，便于分头行动分栏展示。
    if not is_opening and teammates:
        system_content += GROUP_INSTRUCTION

    # 手书（Handouts）：仅当模组尚有未发放的手书、且非开场时，广告 [HANDOUT] 发放能力，
    # 附「id｜类型｜标题｜发放条件」清单（正文不进上下文——发放时才由系统展开成卡片）。
    # 已发放的经线索台账自然呈现；无手书的旧模组不注入本小节（行为不变）。
    if not is_opening:
        try:
            handout_list = _format_handout_list(module, session)
            if handout_list:
                system_content += HANDOUT_INSTRUCTION.format(handout_list=handout_list)
        except Exception:
            logger.exception("手书清单注入 KP 上下文失败（忽略）")

    # 世界记忆注入（fail-open：格式化异常绝不阻塞出牌，退回无记忆行为）——
    # 线索台账：玩家已 known/partial 的线索清单 + 「已列出的不要重复安排发现桥段、
    # 未列出的一律视为未发现」的硬指示；NPC 记忆：各 NPC 的态度/承诺/谎言/最近互动，
    # 保证 NPC 言行跨回合一致。台账/记忆为空时不注入任何小节（与现状完全一致）。
    if not is_opening:
        try:
            ws_mem = session.world_state or {}
            clue_names = {
                c.get("id"): c.get("name")
                for c in (module.clues or []) if c.get("id")
            }
            char_names = {player_char.id: player_char.name}
            char_names.update({t.id: t.name for t in teammates})
            ledger_section = world_memory.format_clue_ledger_section(
                ws_mem, clue_names, char_names,
            )
            if ledger_section:
                system_content += "\n\n" + ledger_section
            npc_memory_section = world_memory.format_npc_memory_section(ws_mem, npcs)
            if npc_memory_section:
                system_content += "\n\n" + npc_memory_section
        except Exception:
            logger.exception("世界记忆注入 KP 上下文失败（忽略）")

    # 幕后动态（Backstage Clock）：最近几条幕后推演事件注入 KP 专属小节。
    # fail-open：格式化异常绝不阻塞出牌；无幕后事件时不注入（与现状完全一致）。
    if not is_opening:
        try:
            backstage_section = _format_backstage_section(events)
            if backstage_section:
                system_content += "\n\n" + backstage_section
        except Exception:
            logger.exception("幕后动态注入 KP 上下文失败（忽略）")

    party_char_ids = {player_char.id} | {t.id for t in teammates}

    system_tokens = _estimate_tokens(system_content)
    if system_tokens > MAX_SYSTEM_TOKENS:
        system_content = _truncate_to_tokens(system_content, MAX_SYSTEM_TOKENS)
        system_tokens = MAX_SYSTEM_TOKENS

    messages = [{"role": "system", "content": system_content}]

    if not events:
        ws = module.world_setting or {}

        # 开场（形式 A）：世界观导入 → 角色亮相 → 踏入起始场景，揉成一段连贯自然的开场白。
        # 前两件按模组数据/在场队伍动态出现，落点恒为 KP_OPENING_PROMPT（场景钩子）。
        # 注意：这些只是「开场白要涵盖的内容」，绝不能在叙述里出现编号或小标题（不着痕迹地融为一体）。
        beats: list[str] = []
        intro = ws.get("intro")
        if intro and str(intro).strip():
            beats.append(
                "用下面这段把全桌带入故事的世界观与基调（可润色营造氛围，但严守无剧透，"
                "绝不提及任何需在游戏中被发现的线索/真相/NPC 秘密）：\n" + str(intro).strip()
            )
        if teammates:
            beats.append(
                f"让在场的 {len(teammates) + 1} 名地位完全平等的玩家角色逐一登场亮相、点出各自"
                "为人所见的公开身份并邀请自我介绍（没有「主角」，不要只对着某一人，"
                "更绝不替任何玩家描写其动作、姿态、心理或台词）"
            )

        if beats:
            opening = (
                "游戏即将开始。请朗读一段**连贯自然**的开场白，把下面几件事不着痕迹地融成一个整体——"
                "像电影开场般顺滑过渡，**不要分点、不要出现编号或「第一/第二」「世界观导入」之类小标题**，"
                "也不要剧透：\n"
                + "；\n".join(beats)
                + "。\n最后自然落到眼前的起始场景——\n"
                + KP_OPENING_PROMPT
            )
        else:
            opening = KP_OPENING_PROMPT

        player_brief = ws.get("player_brief")
        if player_brief and str(player_brief).strip():
            opening += (
                "\n\n【玩家已知背景（player_brief）——开场唯一可作为「玩家已经知道」的钩子】\n"
                + str(player_brief).strip()
                + "\n（除此之外，玩家此刻一无所知；不要把这段之外的任何信息当成玩家已知。）"
            )
        messages.append({"role": "user", "content": opening})
    else:
        event_budget = CONTEXT_TOKEN_BUDGET - system_tokens - RESERVE_FOR_OUTPUT

        # 滚动剧情摘要：已被浓缩进 world_state.story_summary 的老事件（seq ≤ 游标）不再逐条进
        # 上下文，只保留这份持久摘要；游标之后的事件照常按预算给全文，仍超预算的再兜底即时摘要。
        # 游标默认 0（无摘要）→ recent_pool 即全部事件，与旧行为一致（向后兼容）。
        ws = session.world_state or {}
        cursor = ws.get("story_summary_seq") or 0
        persist_summary = (ws.get("story_summary") or "").strip()
        recent_pool = [e for e in events if (e.sequence_num or 0) > cursor]

        all_msgs = _events_to_messages(
            recent_pool, primary_char_id=player_char.id, party_char_ids=party_char_ids,
        )

        if len(all_msgs) <= MIN_RECENT_EVENTS:
            recent_msgs = all_msgs
            older_summary = ""
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
                older_events = recent_pool[:len(recent_pool) - len(recent_msgs)]
                older_summary = _summarize_old_events(older_events, max_tokens=min(MAX_SUMMARY_TOKENS, max(remaining, 500)))
            else:
                older_summary = ""

        summary = "\n".join(s for s in (persist_summary, older_summary) if s)
        if summary:
            messages.append({
                "role": "system",
                "content": "[之前发生的剧情摘要]\n" + summary,
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
    else:
        # 按当前剧情 flags 解析到 NPC 的当前样貌（如已暴露→敌对、已死亡）。
        npc_def = _resolve_state(npc_def, _active_flags(session))

    def _as_text(v) -> str:
        return "\n".join(v) if isinstance(v, list) else (v or "")

    system_content = NPC_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        npc_name=npc_def.get("name", "未知"),
        npc_description=npc_def.get("description", ""),
        npc_background=_as_text(npc_def.get("background")) or "（无特别记述）",
        npc_personality=npc_def.get("personality", "普通人"),
        npc_secrets=_as_text(npc_def.get("secrets")) or "无",
    )

    # 世界记忆：该 NPC 自己的记忆全量注入——记得对玩家的承诺、说过的谎与最近互动，
    # 这是玩家最能感知的一致性改进。无记忆时不注入（向后兼容）；异常 fail-open。
    try:
        self_memory = world_memory.format_npc_self_memory(
            session.world_state or {}, npc_id,
        )
        if self_memory:
            system_content += "\n\n" + self_memory
    except Exception:
        logger.exception("世界记忆注入 NPC 上下文失败（忽略）: npc=%s", npc_id)

    # 信息隔离：NPC 只看「自己所在场景」发生的事，外加显式指向它的事件（visibility 含 npc_id）。
    # 这样一个 NPC 不会知道玩家在别处场景的言行。未打场景戳的旧事件（无 metadata.scene_id）
    # 仍放行以兼容在途存档。注：同一场景内 NPC「到场前」的事件暂不过滤（更细的在场时序留待后续）。
    npc_scene = npc_def.get("initial_location") or session.current_scene_id

    def _npc_can_see(ev: EventLog) -> bool:
        if ev.visibility and npc_id in ev.visibility:
            return True
        if ev.visibility and npc_id not in ev.visibility:
            return False  # 显式限定了可见者且不含本 NPC
        ev_scene = (ev.metadata_ or {}).get("scene_id")
        if not ev_scene:
            return True  # 未打戳的事件兼容放行
        return ev_scene == npc_scene

    visible_events = [ev for ev in events if _npc_can_see(ev)]

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
    separated: bool = False,
    team_guidance: str = "",
) -> list[dict]:
    """构建单个 AI 队友的决策上下文：场景 + 队伍 + 最近事件。

    ``separated``：该队友是否已与大部队分头（身处与主队不同的场景）。分头时下达「主动推进
    本场景」的指引（没人替他推动剧情，全靠自己）；同处一地时仍是「补位与响应、宁缺毋滥」，
    避免抢戏。

    ``team_guidance``：本轮导演对队友的软指引（由 planner 的 direction 派生，如「把话头
    多递给某冷场玩家」）。空则不注入；非空时作为一条 system 提示追加，队友决策仍自主。
    """
    from app.services import session_service  # 局部导入避免顶层循环依赖

    flags = _active_flags(session)
    scenes = [_resolve_state(s, flags) for s in (module.scenes or [])]
    # 用队友「自己所在」的场景（分头后各在各处），而非会话级单一场景
    viewer_scene_id = session_service.get_char_location(session, teammate.id)
    current_scene = _resolve_scene(scenes, viewer_scene_id, flags)
    current_location = (current_scene.get("title") or current_scene.get("name") or "当前所在") if current_scene else "当前所在"

    # 队伍其他成员（一视同仁，无主角；房主角色也只是其中一名队友）
    party_members = [player_char] + [
        t for t in (all_teammates or []) if t.id != teammate.id
    ]
    party_info = "\n".join(f"- 队友：{m.name}" for m in party_members) or "无"

    # 可前往的已知地点（对话提及/已访问；排除当前所在），供 travel 选 target
    known = session_service.list_known_locations(module, session, char_id=teammate.id, events=events)
    known_locations = "、".join(loc["name"] for loc in known if not loc["current"]) or "（暂无其他已知地点）"

    mode_guidance = (
        TEAM_MODE_SEPARATED.format(current_location=current_location)
        if separated
        else TEAM_MODE_TOGETHER
    )
    system_content = TEAM_SYSTEM_PROMPT.format(
        rule_system=module.rule_system.upper(),
        name=teammate.name,
        char_info=_format_player_info(teammate),
        current_location=current_location,
        scene=_format_json(current_scene) if current_scene else "初始场景",
        party_info=party_info,
        known_locations=known_locations,
        mode_guidance=mode_guidance,
    )

    digest = _format_recent_events_digest(
        events[-20:], self_char_id=teammate.id,
    )

    messages = [{"role": "system", "content": system_content}]
    if team_guidance.strip():
        messages.append({
            "role": "system",
            "content": "【本轮导演提示】" + team_guidance.strip(),
        })
    messages.append({
        "role": "user",
        "content": (
            "## 最近发生的事（最新在最后）\n"
            + digest
            + "\n\n轮到你了。请根据队伍刚才的行动和当前局面，"
            "决定你这一回合做什么，并按 JSON 格式输出。"
        ),
    })
    return messages


def _format_recent_events_digest(
    events: list[EventLog],
    self_char_id: str | None = None,
) -> str:
    """把最近事件渲染成给 AI 队友看的纯文本摘要（玩家角色一视同仁，只区分"你"与他人）。"""
    lines: list[str] = []
    for ev in events:
        # 「仅 KP 可见」事件（visibility 含 kp 哨兵，如幕后推演）绝不进玩家侧
        # 队友的上下文——AI 队友是玩家方，看到即泄露。
        if "kp" in (ev.visibility or []):
            continue
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

    # 从「离当前最近」的老事件往前收集，预算用尽即停。旧实现从最早开始塞，长局后摘要会
    # 永远停留在开场附近、丢掉中段与近段剧情，导致 KP 记忆停滞、原地打转、复读开场式内容。
    lines = []
    token_count = 0
    for ev in reversed(events):
        prefix = ev.actor_name or ev.event_type
        snippet = (ev.content or "")[:120].replace("\n", " ")
        line = f"- [{prefix}] {snippet}"
        line_tokens = _estimate_tokens(line)
        if token_count + line_tokens > max_tokens:
            break
        lines.append(line)
        token_count += line_tokens
    lines.reverse()  # 收集是倒序，输出恢复为时间正序

    return "\n".join(lines) if lines else ""
