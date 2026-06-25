from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession


def create_session(db: Session, module_id: str, player_character_id: str) -> GameSession:
    module = db.get(Module, module_id)
    if not module:
        raise ValueError("模组不存在")

    char = db.get(Character, player_character_id)
    if not char:
        raise ValueError("角色不存在")
    if char.module_id != module_id:
        raise ValueError("角色不属于该模组")

    existing = (
        db.query(GameSession)
        .filter(
            GameSession.module_id == module_id,
            GameSession.status.in_(["active", "paused"]),
        )
        .first()
    )
    if existing:
        raise ValueError("该模组已有进行中的游戏，请先结束或继续已有游戏")

    first_scene_id = None
    if module.scenes:
        first_scene_id = module.scenes[0].get("id")

    game_session = GameSession(
        module_id=module_id,
        player_character_id=player_character_id,
        status="active",
        current_scene_id=first_scene_id,
        world_state={"visited_scenes": [first_scene_id] if first_scene_id else []},
    )
    db.add(game_session)
    db.commit()
    db.refresh(game_session)
    return game_session


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
    return (
        db.query(EventLog)
        .filter(EventLog.session_id == session_id)
        .order_by(EventLog.sequence_num.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


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
