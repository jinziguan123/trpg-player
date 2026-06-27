from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant


def _gen_room_code(db: Session) -> str:
    for _ in range(20):
        code = uuid.uuid4().hex[:6].upper()
        if not db.query(GameSession).filter(GameSession.room_code == code).first():
            return code
    return uuid.uuid4().hex[:8].upper()


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
        cid = p.get("character_id")
        if cid:
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
    # 空席（无角色）只能是 human 席
    for s in seats:
        if not s["character_id"]:
            s["role"] = "human"

    primaries = [s for s in seats if s["is_primary"]]
    if not primaries:
        # 取第一个有角色的席位作主角
        filled = [s for s in seats if s["character_id"]]
        if not filled:
            raise ValueError("必须至少有一个已填角色的主角席位")
        filled[0]["is_primary"] = True
        primaries = [filled[0]]
    elif len(primaries) > 1:
        raise ValueError("只能有一个主角席位")
    if not primaries[0]["character_id"]:
        raise ValueError("主角席位必须填入角色")
    # 主角必为真人
    primaries[0]["role"] = "human"
    return seats


def create_session(
    db: Session,
    module_id: str,
    participants: list[dict],
    creator_token: str | None = None,
) -> GameSession:
    module = db.get(Module, module_id)
    if not module:
        raise ValueError("模组不存在")
    if not participants:
        raise ValueError("必须至少提供一个主角席位")

    seats = _normalize_participants(participants)

    for seat in seats:
        if seat["character_id"] and not db.get(Character, seat["character_id"]):
            raise ValueError("角色不存在")

    occupied = active_character_ids(db)
    clash = [
        s["character_id"] for s in seats
        if s["character_id"] and s["character_id"] in occupied
    ]
    if clash:
        raise ValueError("所选角色正在进行其他游戏，请先完成或结束当前游戏")

    primary = next(s for s in seats if s["is_primary"])
    primary_id = primary["character_id"]

    first_scene_id = None
    if module.scenes:
        first_scene_id = module.scenes[0].get("id")

    # 有空的真人席 → 进大厅（setup，等真人认领+准备后房主开局）；
    # 否则（单人/全 AI 已填满）→ 直接 active，保持原快速开局体验。
    has_open_seat = any(
        (not s["character_id"]) and s["role"] == "human" for s in seats
    )
    status = "setup" if has_open_seat else "active"

    game_session = GameSession(
        module_id=module_id,
        player_character_id=primary_id,
        status=status,
        room_code=_gen_room_code(db),
        current_scene_id=first_scene_id,
        world_state={"visited_scenes": [first_scene_id] if first_scene_id else []},
    )
    for order, seat in enumerate(seats):
        claimed = bool(seat["character_id"])
        # 主角席归创建者 token；其它已填真人席暂不预设归属（留给认领或本机）
        owner = creator_token if seat["is_primary"] else None
        # AI 席与房主席默认就绪；空/待认领的真人席需手动准备
        ready = seat["role"] == "ai" or seat["is_primary"]
        game_session.participants.append(
            SessionParticipant(
                character_id=seat["character_id"],
                role=seat["role"],
                is_primary=seat["is_primary"],
                seat_order=order,
                claimed=claimed,
                owner_token=owner,
                ready=ready,
            )
        )
    db.add(game_session)
    # 创建者的主角绑定到其 token
    if creator_token and primary_id:
        char = db.get(Character, primary_id)
        if char and not char.owner_token:
            char.owner_token = creator_token
    db.commit()
    db.refresh(game_session)
    return game_session


def get_session_by_code(db: Session, room_code: str) -> GameSession | None:
    return (
        db.query(GameSession)
        .filter(GameSession.room_code == room_code.upper())
        .first()
    )


def claim_seat(
    db: Session, session_id: str, seat_order: int, character_id: str, token: str,
) -> GameSession:
    """玩家用 token 认领一个空 human 席并带角色入座。"""
    if not token:
        raise ValueError("缺少玩家身份")
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")

    seat = (
        db.query(SessionParticipant)
        .filter(
            SessionParticipant.session_id == session_id,
            SessionParticipant.seat_order == seat_order,
        )
        .first()
    )
    if not seat:
        raise ValueError("席位不存在")
    if seat.role != "human":
        raise ValueError("只能认领真人席位")
    if seat.claimed:
        raise ValueError("该席位已被认领")

    char = db.get(Character, character_id)
    if not char:
        raise ValueError("角色不存在")
    if char.owner_token and char.owner_token != token:
        raise ValueError("该角色属于其他玩家")

    occupied = active_character_ids(db)
    if character_id in occupied:
        raise ValueError("该角色正在进行其他游戏")

    seat.character_id = character_id
    seat.owner_token = token
    seat.claimed = True
    char.owner_token = token
    db.commit()
    db.refresh(session)
    return session


def get_participants(db: Session, session_id: str) -> list[SessionParticipant]:
    return (
        db.query(SessionParticipant)
        .filter(SessionParticipant.session_id == session_id)
        .order_by(SessionParticipant.seat_order.asc())
        .all()
    )


def _primary_seat(db: Session, session_id: str) -> SessionParticipant | None:
    return (
        db.query(SessionParticipant)
        .filter(
            SessionParticipant.session_id == session_id,
            SessionParticipant.is_primary.is_(True),
        )
        .first()
    )


def is_host(db: Session, session_id: str, token: str | None) -> bool:
    """房主 = 主角席的 owner_token 持有者（建房者）。"""
    seat = _primary_seat(db, session_id)
    return bool(token and seat and seat.owner_token == token)


def set_ready(
    db: Session, session_id: str, token: str | None, ready: bool
) -> GameSession:
    """把当前 token 拥有的席位的准备态置位。"""
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")
    seat = (
        db.query(SessionParticipant)
        .filter(
            SessionParticipant.session_id == session_id,
            SessionParticipant.owner_token == token,
        )
        .first()
    )
    if not token or not seat:
        raise ValueError("你不在该房间中")
    seat.ready = bool(ready)
    db.commit()
    db.refresh(session)
    return session


def lobby_gaps(db: Session, session_id: str) -> list[str]:
    """返回开局门槛缺口；空列表代表满足开局条件。"""
    parts = get_participants(db, session_id)
    gaps: list[str] = []
    empty = [p for p in parts if not p.character_id]
    if empty:
        gaps.append(f"还有 {len(empty)} 个空席未填角色")
    not_ready = [
        p for p in parts if p.character_id and p.role == "human" and not p.ready
    ]
    if not_ready:
        gaps.append(f"还有 {len(not_ready)} 名玩家未准备")
    if not any(p.role == "human" and p.character_id for p in parts):
        gaps.append("至少需要 1 名真人玩家")
    return gaps


def start_game(db: Session, session_id: str, token: str | None) -> GameSession:
    """房主校验 + 门槛校验后把房间从 setup 推进到 active。"""
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")
    if session.status != "setup":
        raise ValueError("房间不在大厅状态")
    if not is_host(db, session_id, token):
        raise ValueError("只有房主可以开始游戏")
    gaps = lobby_gaps(db, session_id)
    if gaps:
        raise ValueError("；".join(gaps))
    session.status = "active"
    db.commit()
    db.refresh(session)
    return session


def resolve_actor(
    db: Session, session_id: str, token: str | None, acting_character_id: str | None,
) -> Character:
    """自由式多人：校验并返回本次行动的角色（按 token 校验席位归属）。"""
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")
    target_id = acting_character_id or session.player_character_id
    if not target_id:
        raise ValueError("未指定行动角色")
    parts = get_participants(db, session_id)
    seat = next((p for p in parts if p.character_id == target_id), None)
    if not seat:
        raise ValueError("该角色不在本房间")
    if seat.role != "human":
        raise ValueError("只能以真人席位行动")
    # 席位有归属时校验 token；无归属（旧本机会话）放行
    if seat.owner_token and token and seat.owner_token != token:
        raise ValueError("无权以该角色行动")
    char = db.get(Character, target_id)
    if not char:
        raise ValueError("角色不存在")
    return char


def get_party_members(
    db: Session, session_id: str, exclude_id: str | None = None,
) -> list[Character]:
    """会话内所有已填角色（真人 + AI），可排除某角色；用于 KP 整队上下文。"""
    out: list[Character] = []
    for p in get_participants(db, session_id):
        if not p.character_id or p.character_id == exclude_id:
            continue
        c = db.get(Character, p.character_id)
        if c:
            out.append(c)
    return out


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
