"""战报 recap 编排（P4 5.2a）：加载会话素材 → 生成结构化战报 → 存入 world_state.recaps[]。

fail-open：生成失败（无 LLM / 无事件 / 坏 JSON）返回 None，不落库、不阻塞。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai import recap as recap_ai
from app.ai.llm_factory import get_llm
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import session_service, world_memory

logger = logging.getLogger(__name__)


def _party_status_text(party: list[Character]) -> str:
    """一行行角色现状（HP/SAN/状态），给战报判断阵亡与损失（确定性、不臆造）。"""
    lines: list[str] = []
    for c in party:
        sd = c.system_data or {}
        hp = sd.get("hitPoints") or {}
        san = sd.get("sanity") or {}
        parts = [c.name]
        if hp:
            parts.append(f"HP {hp.get('current', '?')}/{hp.get('max', '?')}")
        if san:
            parts.append(f"SAN {san.get('current', '?')}/{san.get('max', '?')}")
        if getattr(c, "status", None) and c.status != "active":
            parts.append(f"状态：{c.status}")
        lines.append("｜".join(parts))
    return "\n".join(lines)


def list_recaps(db: Session, session_id: str) -> list[dict]:
    session = db.get(GameSession, session_id)
    if session is None:
        return []
    return list((session.world_state or {}).get("recaps") or [])


async def generate_and_store_recap(db: Session, session_id: str) -> dict | None:
    """生成一份战报并追加进 world_state.recaps；成功返回该条，失败返回 None。"""
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
    if not events:
        return None
    teammates = session_service.get_party_members(
        db, session_id, exclude_id=player_char.id,
    )
    party = [player_char] + teammates

    ws = session.world_state or {}
    clue_names = {c.get("id"): c.get("name") for c in (module.clues or []) if c.get("id")}
    char_names = {c.id: c.name for c in party}
    ledger_text = world_memory.format_clue_ledger_section(ws, clue_names, char_names)
    party_status = _party_status_text(party)

    recap = await recap_ai.generate_recap(
        get_llm(),
        prev_summary=ws.get("story_summary") or "",
        events=events,
        clue_ledger_text=ledger_text,
        party_status_text=party_status,
    )
    if recap is None:
        return None

    entry = {
        **recap,
        "up_to_seq": events[-1].sequence_num or 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # JSON 列需整体重新赋值才会被 SQLAlchemy 标脏（in-place 改不触发）。
    ws2 = dict(session.world_state or {})
    ws2["recaps"] = list(ws2.get("recaps") or []) + [entry]
    session.world_state = ws2
    db.commit()
    return entry
