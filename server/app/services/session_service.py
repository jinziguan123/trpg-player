from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant


def active_character_ids(
    db: Session, exclude_session_id: str | None = None
) -> set[str]:
    """返回当前所有活跃/暂停会话占用的角色 id（含主角与 AI 队友）。

    既读旧的 ``player_character_id`` 快捷字段，也读 ``session_participants``，
    供开局冲突校验和 ``/characters?available=true`` 对齐使用。
    """
    q = db.query(GameSession).filter(GameSession.status.in_(["active", "paused"]))
    if exclude_session_id:
        q = q.filter(GameSession.id != exclude_session_id)
    sessions = q.all()
    ids = {s.player_character_id for s in sessions if s.player_character_id}
    session_ids = [s.id for s in sessions]
    if session_ids:
        parts = (
            db.query(SessionParticipant)
            .filter(SessionParticipant.session_id.in_(session_ids))
            .all()
        )
        ids |= {p.character_id for p in parts}
    return ids


def _normalize_participants(participants: list[dict]) -> list[dict]:
    """补全主角标记并强制主角为 human，去重保序。"""
    seen: set[str] = set()
    seats: list[dict] = []
    for p in participants:
        cid = p["character_id"]
        if cid in seen:
            raise ValueError("同一角色不能在同一会话中占据多个席位")
        seen.add(cid)
        seats.append(
            {
                "character_id": cid,
                "role": p.get("role", "ai"),
                "is_primary": bool(p.get("is_primary", False)),
            }
        )
    primaries = [s for s in seats if s["is_primary"]]
    if not primaries:
        seats[0]["is_primary"] = True
        primaries = [seats[0]]
    elif len(primaries) > 1:
        raise ValueError("只能有一个主角席位")
    # 主角必为真人
    primaries[0]["role"] = "human"
    return seats


def create_session(
    db: Session, module_id: str, participants: list[dict]
) -> GameSession:
    module = db.get(Module, module_id)
    if not module:
        raise ValueError("模组不存在")
    if not participants:
        raise ValueError("必须至少提供一个主角席位")

    seats = _normalize_participants(participants)

    for seat in seats:
        if not db.get(Character, seat["character_id"]):
            raise ValueError("角色不存在")

    occupied = active_character_ids(db)
    clash = [s["character_id"] for s in seats if s["character_id"] in occupied]
    if clash:
        raise ValueError("所选角色正在进行其他游戏，请先完成或结束当前游戏")

    primary_id = next(s["character_id"] for s in seats if s["is_primary"])

    first_scene_id = None
    if module.scenes:
        first_scene_id = module.scenes[0].get("id")

    game_session = GameSession(
        module_id=module_id,
        player_character_id=primary_id,
        status="active",
        current_scene_id=first_scene_id,
        world_state={"visited_scenes": [first_scene_id] if first_scene_id else []},
    )
    for order, seat in enumerate(seats):
        game_session.participants.append(
            SessionParticipant(
                character_id=seat["character_id"],
                role=seat["role"],
                is_primary=seat["is_primary"],
                seat_order=order,
            )
        )
    db.add(game_session)
    db.commit()
    db.refresh(game_session)
    return game_session


def get_participants(db: Session, session_id: str) -> list[SessionParticipant]:
    return (
        db.query(SessionParticipant)
        .filter(SessionParticipant.session_id == session_id)
        .order_by(SessionParticipant.seat_order.asc())
        .all()
    )


def get_ai_teammates(db: Session, session_id: str) -> list[Character]:
    """返回会话内所有 AI 队友角色，按席位顺序。"""
    parts = (
        db.query(SessionParticipant)
        .filter(
            SessionParticipant.session_id == session_id,
            SessionParticipant.role == "ai",
        )
        .order_by(SessionParticipant.seat_order.asc())
        .all()
    )
    teammates: list[Character] = []
    for p in parts:
        char = db.get(Character, p.character_id)
        if char:
            teammates.append(char)
    return teammates


def get_session(db: Session, session_id: str) -> GameSession | None:
    return db.get(GameSession, session_id)


def list_sessions(db: Session) -> list[GameSession]:
    return db.query(GameSession).order_by(GameSession.created_at.desc()).all()


def update_session_status(db: Session, session_id: str, status: str) -> GameSession | None:
    session = db.get(GameSession, session_id)
    if not session:
        return None
    session.status = status
    db.commit()
    db.refresh(session)
    return session


def get_session_events(
    db: Session, session_id: str, limit: int = 100, offset: int = 0
) -> list[EventLog]:
    q = (
        db.query(EventLog)
        .filter(EventLog.session_id == session_id)
        .order_by(EventLog.sequence_num.asc())
        .offset(offset)
    )
    if limit > 0:
        q = q.limit(limit)
    return q.all()


def get_latest_events(
    db: Session, session_id: str, limit: int = 50, before_seq: int | None = None,
) -> tuple[list[EventLog], bool]:
    q = db.query(EventLog).filter(EventLog.session_id == session_id)
    if before_seq is not None:
        q = q.filter(EventLog.sequence_num < before_seq)
    q = q.order_by(EventLog.sequence_num.desc())
    rows = q.limit(limit + 1).all()
    has_more = len(rows) > limit
    results = rows[:limit]
    results.reverse()
    return results, has_more


def get_next_sequence_num(db: Session, session_id: str) -> int:
    result = (
        db.query(EventLog.sequence_num)
        .filter(EventLog.session_id == session_id)
        .order_by(EventLog.sequence_num.desc())
        .first()
    )
    return (result[0] + 1) if result else 1


def add_event(
    db: Session,
    session_id: str,
    event_type: str,
    content: str,
    actor_id: str | None = None,
    actor_name: str = "",
    visibility: list[str] | None = None,
    metadata: dict | None = None,
) -> EventLog:
    seq = get_next_sequence_num(db, session_id)
    event = EventLog(
        session_id=session_id,
        sequence_num=seq,
        event_type=event_type,
        actor_id=actor_id,
        actor_name=actor_name,
        content=content,
        visibility=visibility or [],
        metadata_=metadata or {},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def delete_session(db: Session, session_id: str) -> bool:
    session = db.get(GameSession, session_id)
    if not session:
        return False
    db.query(EventLog).filter(EventLog.session_id == session_id).delete()
    db.delete(session)
    db.commit()
    return True


def update_scene(db: Session, session_id: str, scene_id: str) -> None:
    session = db.get(GameSession, session_id)
    if not session:
        return
    session.current_scene_id = scene_id
    ws = dict(session.world_state or {})
    visited = ws.get("visited_scenes", [])
    if scene_id not in visited:
        visited.append(scene_id)
    ws["visited_scenes"] = visited
    session.world_state = ws
    db.commit()
