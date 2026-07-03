"""上下文占用预估：给某会话算出「下一回合 KP 上下文」的分项 token 估算，
与模型上下文窗口对比，帮用户判断模型是否还撑得住继续跑团（也是后续上下文压缩的基础）。

纯确定性、零 LLM 调用：复用 build_kp_context 组装真实上下文（不含按需 RAG 摘录——那部分
由生成时现检索，量有界，这里作为「基础估算」不计入，返回里标注），再用 _estimate_tokens 分项累加。
fail-open：任何异常都不该影响跑团；调用方（API）自行兜底。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.context import (
    CONTEXT_TOKEN_BUDGET,
    RESERVE_FOR_OUTPUT,
    _estimate_tokens,
    build_kp_context,
)
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import rulebook_service, session_service

_SUMMARY_PREFIX = "[之前发生的剧情摘要]"


def _status(ratio: float) -> str:
    """按「输入+输出预留 / 窗口」占比给健康度：正常 / 偏紧 / 濒临溢出。"""
    if ratio >= 0.95:
        return "critical"
    if ratio >= 0.8:
        return "warn"
    return "ok"


def estimate_session_context(db: Session, session_id: str) -> dict | None:
    """返回该会话下一回合 KP 上下文的 token 预估与窗口占比；会话/模组缺失返回 None。"""
    from app.api.ai_settings import load_active_profile, resolve_context_window

    session = db.get(GameSession, session_id)
    if session is None:
        return None
    module = db.get(Module, session.module_id) if session.module_id else None
    player_char = (
        db.get(Character, session.player_character_id)
        if session.player_character_id else None
    )
    if module is None or player_char is None:
        return None

    events = session_service.get_session_events(db, session_id, limit=0)
    teammates = session_service.get_party_members(
        db, session_id, exclude_id=session.player_character_id,
    )
    rules_enabled = bool(events) and rulebook_service.has_rulebook(db, module.rule_system)
    module_rag_enabled = bool(events) and getattr(module, "rag_status", "") == "ready"

    messages = build_kp_context(
        session, module, player_char, events,
        teammates=teammates or None,
        rules_lookup_enabled=rules_enabled,
        module_lookup_enabled=module_rag_enabled,
    )

    # 分项：system（KP 系统提示 + 模组数据 + 台账/记忆/幕后）、summary（滚动剧情摘要）、
    # history（近期逐条事件 + 少量格式提醒）。三者相加 = 本回合送入模型的输入估算。
    system_tokens = 0
    summary_tokens = 0
    history_tokens = 0
    for i, m in enumerate(messages):
        toks = _estimate_tokens(m.get("content") or "")
        role = m.get("role")
        content = m.get("content") or ""
        if i == 0 and role == "system":
            system_tokens += toks
        elif role == "system" and content.startswith(_SUMMARY_PREFIX):
            summary_tokens += toks
        else:
            history_tokens += toks

    input_tokens = system_tokens + summary_tokens + history_tokens

    profile = load_active_profile()
    context_window = resolve_context_window(profile)
    required = input_tokens + RESERVE_FOR_OUTPUT
    ratio = round(required / context_window, 4) if context_window else 0.0

    # 压缩指标：滚动摘要游标之后的事件才可能逐条进上下文，游标之前的已被浓缩进 story_summary。
    ws = session.world_state or {}
    cursor = ws.get("story_summary_seq") or 0
    total_events = len(events)
    summarized_events = sum(1 for e in events if (e.sequence_num or 0) <= cursor)

    return {
        "model": profile.model_name if profile else "unknown",
        "context_window": context_window,
        "context_budget": CONTEXT_TOKEN_BUDGET,   # 组装时的硬上限：超出即摘要/截断
        "output_reserve": RESERVE_FOR_OUTPUT,
        "input_tokens": input_tokens,
        "breakdown": {
            "system": system_tokens,
            "summary": summary_tokens,
            "history": history_tokens,
        },
        "events": {
            "total": total_events,
            "summarized": summarized_events,        # 已并入滚动摘要（非逐条）
            "verbatim_candidates": total_events - summarized_events,
        },
        "usage_ratio": ratio,                       # (输入+输出预留)/窗口
        "status": _status(ratio),
        # 说明：估算未计入按需检索的规则/模组原文摘录（生成时现检索、量有界）。
        "excludes_rag_excerpts": True,
    }
