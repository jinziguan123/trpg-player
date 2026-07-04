"""临场 NPC 受控转正编排（P2）：把一个已登记的临场龙套，据其既有言行生成完整 NPC 卡，
挂到 world_state.improvised_npcs[name].card（会话级，不写模组本体）。

只由房主显式触发（API 层鉴权），绝不自动转正。fail-open：生成失败返回 None、不落库。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ai import npc_promote
from app.ai.llm_factory import get_llm
from app.models.module import Module
from app.models.session import GameSession
from app.services import session_service, world_memory

logger = logging.getLogger(__name__)


def list_improvised(db: Session, session_id: str) -> list[dict]:
    """列出本局临场 NPC：[{name, mentions, promoted(bool)}]，供房主决定是否转正。"""
    session = db.get(GameSession, session_id)
    if session is None:
        return []
    improv = (session.world_state or {}).get("improvised_npcs") or {}
    out: list[dict] = []
    for name, entry in improv.items():
        entry = entry if isinstance(entry, dict) else {}
        out.append({
            "name": name,
            "mentions": int(entry.get("mentions", 0)),
            "promoted": bool((entry.get("card") or {}).get("id")),
        })
    return out


async def promote(db: Session, session_id: str, name: str) -> dict | None:
    """把临场 NPC `name` 转正：生成 NPC 卡并存入 world_state。成功返回该卡，失败返回 None。"""
    name = (name or "").strip()
    session = db.get(GameSession, session_id)
    if session is None or not name:
        return None
    improv = (session.world_state or {}).get("improvised_npcs") or {}
    if name not in improv:
        return None  # 未登记的名字不给转正（只能转正确实临场出现过的龙套）
    module = db.get(Module, session.module_id) if session.module_id else None

    events = session_service.get_session_events(db, session_id, limit=0)
    material = npc_promote.collect_npc_material(events, name)
    card = await npc_promote.generate_npc_card(
        get_llm(), name=name, material=material,
        module_title=(module.title if module else ""),
    )
    if card is None:
        return None

    ws = world_memory.promote_improvised_npc(dict(session.world_state or {}), name, card)
    session.world_state = ws
    db.commit()
    return ws["improvised_npcs"][name]["card"]
