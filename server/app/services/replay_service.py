"""团记导出编排（P4 5.2b）：加载玩家可见事件流 → 按 token 分窗 → 逐窗改写 → 拼 markdown。

纯离线批处理，不走跑团主链路。每窗 fail-open：改写失败回退朴素文本，导出始终完整。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai import replay as replay_ai
from app.ai.context import _estimate_tokens
from app.ai.llm_factory import get_llm
from app.ai.story_summarizer import _events_text
from app.models.module import Module
from app.models.session import GameSession
from app.services import session_service

VALID_STYLES = ("novel", "script")
_WINDOW_TOKEN_BUDGET = 1500       # 单窗输入事件的 token 预算


def _window_events(events: list) -> list[list]:
    """把事件按 ~1500 token 切成若干窗（每窗至少 1 条，超预算即断窗）。"""
    windows: list[list] = []
    cur: list = []
    cur_tokens = 0
    for e in events:
        t = _estimate_tokens(getattr(e, "content", "") or "")
        if cur and cur_tokens + t > _WINDOW_TOKEN_BUDGET:
            windows.append(cur)
            cur, cur_tokens = [], 0
        cur.append(e)
        cur_tokens += t
    if cur:
        windows.append(cur)
    return windows


def _visible_story_events(db: Session, session_id: str) -> list:
    """玩家可见的剧情事件：旁白/对话/行动/掷骰，排除仅 KP 可见（幕后等）。"""
    events = session_service.get_session_events(db, session_id, limit=0)
    return [
        e for e in events
        if getattr(e, "event_type", None) in ("narration", "dialogue", "action", "dice")
        and not session_service.is_kp_only_event(e)
    ]


async def export_replay(db: Session, session_id: str, style: str) -> dict | None:
    """把整局改写成 markdown 团记；会话/模组缺失或无事件返回 None。"""
    if style not in VALID_STYLES:
        style = "novel"
    session = db.get(GameSession, session_id)
    if session is None:
        return None
    module = db.get(Module, session.module_id) if session.module_id else None

    events = _visible_story_events(db, session_id)
    if not events:
        return None

    llm = get_llm()
    windows = _window_events(events)
    parts: list[str] = []
    prev_tail = ""
    for win in windows:
        body = _events_text(win)
        rewritten = await replay_ai.rewrite_window(llm, style, body, prev_tail)
        piece = rewritten or body   # fail-open：该窗回退朴素记录
        parts.append(piece)
        prev_tail = replay_ai.tail_of(piece)

    title = (module.title if module else None) or "跑团团记"
    style_cn = "小说体" if style == "novel" else "剧本体"
    markdown = f"# {title}（{style_cn}团记）\n\n" + "\n\n".join(parts) + "\n"
    return {"style": style, "title": title, "markdown": markdown, "windows": len(windows)}
