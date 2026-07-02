"""世界记忆层 v1：线索台账 + NPC 记忆（纯确定性来源，零额外 LLM 调用）。

把「玩家知道了什么」「NPC 记得什么」从上下文推断变成 world_state 里的持久化结构：

- ``clue_ledger``：只记录已被触碰的线索（未触碰 = 不在字典里），
  status 二态：partial（有所察觉）/ known（完全掌握），known 不降级。
- ``npc_memory``：每个 NPC 的态度 / 承诺 / 谎言 / 最近互动，
  interactions 是环形缓冲（最多 ``MAX_NPC_INTERACTIONS`` 条），防 world_state 无限膨胀。

本模块全部是「读-改-写 world_state dict」的纯函数：不触碰数据库、不修改入参，
返回一份更新后的拷贝，由调用方整 dict 回写 ``session.world_state``——SQLAlchemy 的
JSON 列就地修改不被追踪，必须整体重新赋值才会落库。
"""

from __future__ import annotations

# interactions 环形缓冲上限
MAX_NPC_INTERACTIONS = 8
# 台账备注 / 互动摘要的截断长度（确定性来源直接截原文，不调 LLM 浓缩）
NOTE_MAX_CHARS = 80

# TurnPlan.clue_policy.reveal_level → 台账状态：hint=有所察觉，direct=完全掌握。
# 规划器偶尔写出 full/known 等同义词，宽容归一；无法识别的非 none 值按 partial 保守处理
# （partial 可再升级为 known，不会错误地阻止后续完整揭示）。
_REVEAL_TO_STATUS = {
    "hint": "partial",
    "partial": "partial",
    "direct": "known",
    "full": "known",
    "known": "known",
}

_STATUS_LABEL = {"partial": "有所察觉", "known": "完全掌握"}
_ATTITUDE_LABEL = {
    "hostile": "敌视",
    "wary": "警惕",
    "neutral": "中立",
    "warming": "好感渐增",
    "trusting": "信任",
}


def _truncate(text, limit: int = NOTE_MAX_CHARS) -> str:
    text = str(text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def reveal_status(reveal_level) -> str | None:
    """把 clue_policy.reveal_level 归一成台账状态；none/空 = 本轮不揭示，返回 None。"""
    lvl = str(reveal_level or "").strip().lower()
    if not lvl or lvl == "none":
        return None
    return _REVEAL_TO_STATUS.get(lvl, "partial")


def record_clue_reveal(
    ws: dict,
    clue_ids: list[str],
    reveal_level: str,
    discovered_by: list[str],
    seq: int,
    note: str = "",
) -> dict:
    """把一次线索揭示写入台账（partial ← hint，known ← direct）。

    known 不降级：已完全掌握的线索不会因为后续 hint 退回 partial；
    ``discovered_by`` 增量合并去重（分头行动下信息不共享，谁在场谁知道）；
    ``seq`` 只记首次触碰时的事件序号。
    """
    status = reveal_status(reveal_level)
    if status is None or not clue_ids:
        return ws
    ws = dict(ws or {})
    ledger = dict(ws.get("clue_ledger") or {})
    for cid in clue_ids:
        cid = str(cid or "").strip()
        if not cid:
            continue
        entry = dict(ledger.get(cid) or {})
        entry["status"] = "known" if entry.get("status") == "known" else status
        known_by = list(entry.get("discovered_by") or [])
        for who in discovered_by or []:
            if who and who not in known_by:
                known_by.append(who)
        entry["discovered_by"] = known_by
        entry["seq"] = entry.get("seq") or int(seq or 0)
        if note:
            entry["note"] = _truncate(note)
        ledger[cid] = entry
    ws["clue_ledger"] = ledger
    return ws


def record_npc_interaction(ws: dict, npc_id: str, seq: int, summary: str) -> dict:
    """给某 NPC 的互动史追加一条（环形缓冲，只保留最近 ``MAX_NPC_INTERACTIONS`` 条）。"""
    npc_id = str(npc_id or "").strip()
    summary = _truncate(summary)
    if not npc_id or not summary:
        return ws
    ws = dict(ws or {})
    memory = dict(ws.get("npc_memory") or {})
    entry = dict(memory.get(npc_id) or {})
    interactions = list(entry.get("interactions") or [])
    interactions.append({"seq": int(seq or 0), "summary": summary})
    entry["interactions"] = interactions[-MAX_NPC_INTERACTIONS:]
    memory[npc_id] = entry
    ws["npc_memory"] = memory
    return ws


# 抽取器允许写入的 NPC 态度枚举——超出集合的值视为幻觉，丢弃不写。
_VALID_ATTITUDES = set(_ATTITUDE_LABEL)


def _append_unique(existing, additions) -> list[str]:
    """把 ``additions`` 里的非空文本追加进 ``existing`` 列表，去重且保留原顺序。"""
    out = [str(p) for p in (existing or []) if str(p).strip()]
    seen = set(out)
    for item in additions or []:
        item = str(item or "").strip()
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def apply_memory_delta(
    ws: dict,
    npc_updates: dict | None = None,
    clue_notes: dict | None = None,
) -> dict:
    """把 MemoryKeeper 抽取器产出的差量合并进 world_state（v2，低温兜底）。

    安全边界（防抽取器幻觉污染确定性台账）：
    - **只允许改已存在的 NPC**：``npc_updates`` 里不在 ``npc_memory`` 的 key 一律忽略——
      NPC 记忆的「诞生」仍由 v1 确定性钩子（说话/被看穿）负责，抽取器只做增量修饰。
    - 每个 NPC 只允许改 ``attitude``（须落在枚举内）/ ``attitude_reason`` /
      追加 ``new_promises`` 到 ``promises`` / 追加 ``new_lies`` 到 ``lies_told``；
      追加均去重保序，绝不触碰 ``interactions`` 环形缓冲。
    - **严禁改 ``clue_ledger`` 的 status**：``clue_notes`` 只允许更新**已存在**线索条目的
      ``note`` 备注，绝不新建条目、绝不改状态——玩家是否已知一律以 v1 确定性来源为准。

    纯函数：不改入参，返回更新后的新 dict；无有效差量则原样返回（保持原记忆不变）。
    """
    ws = dict(ws or {})
    if npc_updates:
        memory = dict(ws.get("npc_memory") or {})
        changed = False
        for nid, upd in npc_updates.items():
            nid = str(nid or "").strip()
            # 只修饰已存在的 NPC：抽取器不得凭空造出未被玩家触碰过的 NPC 记忆
            if not nid or nid not in memory or not isinstance(upd, dict):
                continue
            entry = dict(memory[nid])
            attitude = str(upd.get("attitude") or "").strip().lower()
            if attitude in _VALID_ATTITUDES:
                entry["attitude"] = attitude
                reason = str(upd.get("attitude_reason") or "").strip()
                if reason:
                    entry["attitude_reason"] = _truncate(reason)
            entry["promises"] = _append_unique(
                entry.get("promises"), upd.get("new_promises"),
            )
            entry["lies_told"] = _append_unique(
                entry.get("lies_told"), upd.get("new_lies"),
            )
            memory[nid] = entry
            changed = True
        if changed:
            ws["npc_memory"] = memory
    if clue_notes:
        ledger = dict(ws.get("clue_ledger") or {})
        changed = False
        for cid, note in clue_notes.items():
            cid = str(cid or "").strip()
            note = _truncate(note)
            # 只更新已存在线索的备注：不新建条目、不碰 status（是否已知以确定性来源为准）
            if not cid or cid not in ledger or not note:
                continue
            entry = dict(ledger[cid])
            entry["note"] = note
            ledger[cid] = entry
            changed = True
        if changed:
            ws["clue_ledger"] = ledger
    return ws


def discovered_clue_status(ws: dict) -> dict[str, str]:
    """{clue_id: status}，只含已被触碰的线索——给 planner 做 candidate 过滤输入。"""
    out: dict[str, str] = {}
    for cid, entry in dict((ws or {}).get("clue_ledger") or {}).items():
        status = (entry or {}).get("status")
        if status in ("partial", "known"):
            out[str(cid)] = status
    return out


def npc_memory_of(ws: dict, npc_id: str) -> dict:
    return dict(dict((ws or {}).get("npc_memory") or {}).get(str(npc_id)) or {})


def format_clue_ledger_section(
    ws: dict,
    clue_names: dict[str, str] | None = None,
    char_names: dict[str, str] | None = None,
) -> str:
    """渲染 KP 上下文的「线索台账」小节；台账为空返回空串（向后兼容，不注入）。"""
    ledger = dict((ws or {}).get("clue_ledger") or {})
    if not ledger:
        return ""
    clue_names = clue_names or {}
    char_names = char_names or {}
    lines = ["【线索台账】（内部资料——玩家已掌握的线索进度，绝不向玩家复述本清单或线索 id）"]
    for cid in sorted(ledger):
        entry = ledger[cid] or {}
        label = _STATUS_LABEL.get(entry.get("status"), str(entry.get("status") or ""))
        name = clue_names.get(cid)
        head = f"{name}（{cid}）" if name else cid
        who = "、".join(char_names.get(w, w) for w in (entry.get("discovered_by") or []))
        line = f"- {head}：{label}" + (f"｜知晓者：{who}" if who else "")
        if entry.get("note"):
            line += f"｜备注：{entry['note']}"
        lines.append(line)
    lines.append(
        "已列出的线索玩家已经掌握（或有所察觉），不要重复安排「发现」桥段、"
        "不要再把它当成新信息揭示；未列出的线索一律视为未发现，照常守密。"
    )
    return "\n".join(lines)


def format_npc_memory_brief(ws: dict, npc_id: str) -> str:
    """单个 NPC 记忆的一行摘要（态度/承诺/谎言/最近互动），无记忆返回空串。"""
    mem = npc_memory_of(ws, npc_id)
    if not mem:
        return ""
    parts: list[str] = []
    attitude = str(mem.get("attitude") or "").strip()
    if attitude:
        reason = str(mem.get("attitude_reason") or "").strip()
        parts.append(
            f"态度：{_ATTITUDE_LABEL.get(attitude, attitude)}"
            + (f"（{reason}）" if reason else "")
        )
    promises = [str(p) for p in (mem.get("promises") or []) if str(p).strip()]
    if promises:
        parts.append("承诺过：" + "；".join(promises))
    lies = [str(p) for p in (mem.get("lies_told") or []) if str(p).strip()]
    if lies:
        parts.append("说过的谎：" + "；".join(lies))
    recent = [
        str((i or {}).get("summary") or "").strip()
        for i in (mem.get("interactions") or [])[-3:]
    ]
    recent = [r for r in recent if r]
    if recent:
        parts.append("最近互动：" + "；".join(recent))
    return "。".join(parts)


def format_npc_memory_all_brief(ws: dict, npc_names: dict[str, str] | None = None) -> str:
    """把 npc_memory 里所有 NPC 的记忆各渲一行，喂给 MemoryKeeper 抽取器当输入。

    与需要 npc_defs 的 ``format_npc_memory_section`` 不同：抽取点（滚动摘要处）手边未必有
    module，这里直接遍历记忆字典，行首用 npc_id（抽取器差量的 key 必须是 id），可选带上名字。
    无记忆返回空串。
    """
    memory = dict((ws or {}).get("npc_memory") or {})
    if not memory:
        return ""
    names = npc_names or {}
    lines: list[str] = []
    for nid in sorted(memory):
        brief = format_npc_memory_brief(ws, nid)
        name = names.get(nid)
        head = f"{nid}（{name}）" if name else nid
        lines.append(f"- {head}：{brief}" if brief else f"- {head}：（暂无记忆细节）")
    return "\n".join(lines)


def format_npc_memory_section(ws: dict, npc_defs: list[dict] | None) -> str:
    """KP 上下文的「NPC 记忆」小节：只列有记忆的 NPC（有记忆＝已被玩家触碰过）。"""
    memory = dict((ws or {}).get("npc_memory") or {})
    if not memory:
        return ""
    lines: list[str] = []
    for npc in npc_defs or []:
        nid = npc.get("id")
        if not nid or nid not in memory:
            continue
        brief = format_npc_memory_brief(ws, nid)
        if brief:
            lines.append(f"- {npc.get('name') or nid}：{brief}")
    if not lines:
        return ""
    return (
        "【NPC 记忆】（各 NPC 记得的过往——他们记得对玩家的承诺与自己说过的谎，"
        "其言行必须与之一致，不得凭空遗忘或自相矛盾）\n" + "\n".join(lines)
    )


def format_npc_self_memory(ws: dict, npc_id: str) -> str:
    """给 ``build_npc_context`` 的「你的记忆」小节：该 NPC 自己的记忆全量注入。"""
    mem = npc_memory_of(ws, npc_id)
    if not mem:
        return ""
    lines = ["【你的记忆】（你清楚地记得下面这些事，言行必须与之一致）"]
    attitude = str(mem.get("attitude") or "").strip()
    if attitude:
        reason = str(mem.get("attitude_reason") or "").strip()
        lines.append(
            f"你对这队调查者的态度：{_ATTITUDE_LABEL.get(attitude, attitude)}"
            + (f"——{reason}" if reason else "")
        )
    promises = [str(p) for p in (mem.get("promises") or []) if str(p).strip()]
    if promises:
        lines.append("你许下过的承诺（要记得兑现或找借口拖延）：" + "；".join(promises))
    lies = [str(p) for p in (mem.get("lies_told") or []) if str(p).strip()]
    if lies:
        lines.append("你说过的谎（不要自相矛盾，也不要轻易承认）：" + "；".join(lies))
    interactions = [
        str((i or {}).get("summary") or "").strip()
        for i in (mem.get("interactions") or [])
    ]
    interactions = [i for i in interactions if i]
    if interactions:
        lines.append("最近与你有关的事（从旧到新）：" + "；".join(interactions))
    return "\n".join(lines) if len(lines) > 1 else ""
