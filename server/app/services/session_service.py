from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant

# 「仅 KP 可见」的 visibility 哨兵：带此哨兵的事件（如幕后推演）只进 KP 上下文，
# 对一切玩家侧出口（历史/重连分页、搜索、AI 队友上下文、NPC 上下文、广播）全部不可见。
KP_ONLY_SENTINEL = "kp"


def is_kp_only_event(ev: EventLog) -> bool:
    """该事件是否「仅 KP 可见」（visibility 含 kp 哨兵）——玩家侧查询一律过滤。"""
    return KP_ONLY_SENTINEL in (ev.visibility or [])


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
    *,
    commit: bool = True,
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
    if commit:
        db.commit()
        db.refresh(game_session)
    else:
        db.flush()
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


# ── 结束模组：全体非AI玩家共识投票 ──────────────────────────────────
# 结束不再由房主单方拍板，而是「所有真人玩家一致同意」才执行（单人时自然退化为一键结束）。
# 投票者口径与回合确认（turn_confirm）一致：所有已填角色的真人席，按 character_id 计票；
# 投票经 resolve_actor 按 token 校验席位归属。按 A 方案不做掉线豁免（严格全体一致）。

def _end_voter_ids(db: Session, session_id: str) -> set[str]:
    """有资格参与结束投票的角色 id = 所有已填角色的真人席（与回合确认口径一致）。"""
    return {
        p.character_id for p in get_participants(db, session_id)
        if p.role == "human" and p.character_id
    }


def end_vote_public(db: Session, session_id: str) -> dict:
    """对外可见的结束投票态：每个真人玩家是否已同意、已同意数 / 总数、是否进行中。"""
    session = db.get(GameSession, session_id)
    voter_ids = _end_voter_ids(db, session_id)
    agreed = (
        set((session.world_state or {}).get("end_vote", {}).get("agreed") or []) & voter_ids
        if session else set()
    )
    names = {
        c.id: c.name
        for c in db.query(Character).filter(Character.id.in_(voter_ids)).all()
    } if voter_ids else {}
    voters = [
        {"character_id": cid, "name": names.get(cid, "玩家"), "agreed": cid in agreed}
        for cid in sorted(voter_ids)
    ]
    return {
        "open": len(agreed) > 0,
        "voters": voters,
        "agreed_count": len(agreed),
        "total": len(voter_ids),
    }


def cast_end_vote(
    db: Session, session_id: str, token: str | None, acting_character_id: str | None,
) -> tuple[bool, dict]:
    """兼容入口：校验 token 后由纯业务投票函数执行。"""
    actor = resolve_actor(db, session_id, token, acting_character_id)
    return cast_end_vote_for_actor(db, session_id, actor.id)


def cast_end_vote_for_actor(
    db: Session,
    session_id: str,
    actor_id: str,
) -> tuple[bool, dict]:
    """以已授权真人角色投票；返回 (是否结束, 公开投票态)。"""
    from app.services import world_state

    voter_ids = _end_voter_ids(db, session_id)
    if actor_id not in voter_ids:
        raise ValueError("只有真人玩家可参与结束投票")
    session = db.get(GameSession, session_id)
    ev = dict((session.world_state or {}).get("end_vote") or {})
    agreed = (set(ev.get("agreed") or []) | {actor_id}) & voter_ids
    world_state.set_key(db, session, "end_vote", {"agreed": sorted(agreed)})
    if agreed >= voter_ids:                                            # 全体真人一致同意
        update_session_status(db, session_id, "ended")
        world_state.set_key(db, db.get(GameSession, session_id), "end_vote", None)
        return True, end_vote_public(db, session_id)
    return False, end_vote_public(db, session_id)


def cancel_end_vote(
    db: Session, session_id: str, token: str | None, acting_character_id: str | None,
) -> dict:
    """兼容入口：校验 token 后由纯业务撤票函数执行。"""
    actor = resolve_actor(db, session_id, token, acting_character_id)
    return cancel_end_vote_for_actor(db, session_id, actor.id)


def cancel_end_vote_for_actor(
    db: Session,
    session_id: str,
    actor_id: str,
) -> dict:
    """由已授权真人角色撤销进行中的结束投票。"""
    from app.services import world_state

    if actor_id not in _end_voter_ids(db, session_id):
        raise ValueError("只有真人玩家可撤销结束投票")
    world_state.set_key(db, db.get(GameSession, session_id), "end_vote", None)
    return end_vote_public(db, session_id)


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


def kick_seat(
    db: Session, session_id: str, seat_order: int, token: str | None
) -> tuple[GameSession, str]:
    """房主把某真人席位的玩家移出，席位回到空席待认领。返回 (session, 被踢角色名)。"""
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")
    if session.status != "setup":
        raise ValueError("游戏已开始，无法移出席位")
    if not is_host(db, session_id, token):
        raise ValueError("只有房主可以移出玩家")
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
    if seat.is_primary:
        raise ValueError("不能移出房主自己")
    if seat.role != "human":
        raise ValueError("只能移出真人玩家")
    char = db.get(Character, seat.character_id) if seat.character_id else None
    name = char.name if char else "玩家"
    seat.character_id = None
    seat.owner_token = None
    seat.claimed = False
    seat.ready = False
    db.commit()
    db.refresh(session)
    return session, name


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
    # 席位有归属时必须校验 token：缺 token 或不匹配一律拒绝（此前『token 为空即放行』
    # 会让攻击者不带 X-Player-Token 头就冒充任意有主席位）。无归属席位（纯本机旧会话）放行。
    if seat.owner_token and seat.owner_token != (token or ""):
        raise ValueError("无权以该角色行动")
    char = db.get(Character, target_id)
    if not char:
        raise ValueError("角色不存在")
    return char


def resolve_token_actor(
    db: Session,
    session_id: str,
    token: str | None,
) -> Character:
    """按 token 解析当前真人角色；纯本机旧会话回落到主角。"""
    session = db.get(GameSession, session_id)
    if not session:
        raise ValueError("房间不存在")

    parts = get_participants(db, session_id)
    if token:
        seat = next(
            (
                p
                for p in parts
                if p.owner_token == token and p.role == "human" and p.character_id
            ),
            None,
        )
        if seat:
            char = db.get(Character, seat.character_id)
            if not char:
                raise ValueError("角色不存在")
            return char

    # 只要会话已有任何席位归属，就不能把缺失或错误 token 回退成主角。
    if any(p.owner_token for p in parts):
        raise ValueError("无权以该角色行动")

    target_id = session.player_character_id
    if not target_id:
        raise ValueError("未指定行动角色")
    seat = next((p for p in parts if p.character_id == target_id), None)
    if seat and seat.role != "human":
        raise ValueError("只能以真人席位行动")
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


def is_human_controlled(db: Session, session_id: str, char_id: str | None) -> bool:
    """该角色是否由真人控制（用于决定检定是「待玩家投骰」还是系统自动掷）。

    有 human 席位认领该角色即真人；找不到席位时，主角默认按真人处理（兼容未建席位的旧会话）。
    """
    if not char_id:
        return False
    part = (
        db.query(SessionParticipant)
        .filter(
            SessionParticipant.session_id == session_id,
            SessionParticipant.character_id == char_id,
        )
        .first()
    )
    if part is not None:
        return part.role == "human"
    sess = db.get(GameSession, session_id)
    return bool(sess and sess.player_character_id == char_id)


def add_pending_check(db: Session, session_id: str, check: dict) -> None:
    """登记一个「待玩家投骰」的检定（world_state.pending_checks，按 check_id 存）。"""
    session = db.get(GameSession, session_id)
    if not session:
        return
    ws = dict(session.world_state or {})
    pending = dict(ws.get("pending_checks") or {})
    pending[check["id"]] = check
    ws["pending_checks"] = pending
    session.world_state = ws
    db.commit()


def get_pending_check(
    db: Session,
    session_id: str,
    check_id: str,
) -> dict | None:
    """按 id 读取待投检定，不移除状态。"""
    session = db.get(GameSession, session_id)
    if not session:
        return None
    pending = (session.world_state or {}).get("pending_checks") or {}
    check = pending.get(check_id)
    return dict(check) if isinstance(check, dict) else None


def find_pending_check(
    db: Session, session_id: str, char_id: str | None, skill: str, difficulty: str,
) -> dict | None:
    """查是否已存在等价的待投检定（同 角色+技能+难度）。用于去重——分头行动下同一 plan 注入
    每个分组，多组会各自吐出同一条 [DICE_CHECK]，合并处理会重复挂 pending / 弹重复投骰卡。"""
    session = db.get(GameSession, session_id)
    if not session:
        return None
    pending = (session.world_state or {}).get("pending_checks") or {}
    for c in pending.values():
        if (
            c.get("char_id") == char_id
            and c.get("skill") == skill
            and (c.get("difficulty") or "normal") == (difficulty or "normal")
        ):
            return c
    return None


def pop_pending_check(db: Session, session_id: str, check_id: str) -> dict | None:
    """取出并移除一个待定检定；不存在返回 None。"""
    session = db.get(GameSession, session_id)
    if not session:
        return None
    ws = dict(session.world_state or {})
    pending = dict(ws.get("pending_checks") or {})
    check = pending.pop(check_id, None)
    if check is None:
        return None
    ws["pending_checks"] = pending
    session.world_state = ws
    db.commit()
    return check


def rollback_last_kp_output(db: Session, session_id: str) -> int:
    """回滚「最新一次 KP 会话」的叙事产物，供玩家「重新生成」用。

    删除范围 = 最后一条『玩家方（真人玩家 + AI 队友）行动/发言』之后的：
      - KP 旁白（narration）
      - NPC 台词（dialogue 且行动者不属于玩家方）
      - 待玩家投骰的检定请求（system + metadata.check_request），并清掉对应 pending_checks
    刻意**保留**：玩家/队友的行动与发言、已投出的骰子结果（dice，不重掷）、HP/场景等其他 system。

    这样「重新生成」= 拿本轮玩家与队友的既有输入、以及已定的骰子，重新生成 KP 叙事，
    而不会重跑队友回合、也不会重掷已定的检定。返回删除的事件条数。
    """
    session = db.get(GameSession, session_id)
    if not session:
        return 0
    party_ids = {
        p.character_id
        for p in db.query(SessionParticipant)
        .filter(SessionParticipant.session_id == session_id)
        .all()
    }
    if session.player_character_id:
        party_ids.add(session.player_character_id)

    events = get_session_events(db, session_id, limit=0)
    last_input = -1
    for i, ev in enumerate(events):
        if ev.event_type in ("action", "dialogue") and ev.actor_id in party_ids:
            last_input = i

    removed = 0
    removed_check_ids: list[str] = []
    for ev in events[last_input + 1:]:
        meta = ev.metadata_ or {}
        is_narration = ev.event_type == "narration"
        is_npc_dialogue = ev.event_type == "dialogue" and ev.actor_id not in party_ids
        is_check_request = ev.event_type == "system" and meta.get("check_request")
        if not (is_narration or is_npc_dialogue or is_check_request):
            continue
        if is_check_request and meta.get("id"):
            removed_check_ids.append(meta["id"])
        db.delete(ev)
        removed += 1

    if removed_check_ids:
        ws = dict(session.world_state or {})
        pending = dict(ws.get("pending_checks") or {})
        for cid in removed_check_ids:
            pending.pop(cid, None)
        ws["pending_checks"] = pending
        session.world_state = ws

    if removed:
        db.commit()
    return removed


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


def list_sessions_for_token(
    db: Session, token: str | None
) -> list[GameSession]:
    """按 token 过滤为「我参与的会话」，避免客人连上主机后看到房主的全部私有存档。

    可见规则：
      - 会话内没有任何有主席位（纯本机/旧会话，无归属）→ 本机可见，保持原体验；
      - 否则仅当 token 拥有其中某个席位时可见。
    """
    out: list[GameSession] = []
    for s in list_sessions(db):
        owner_tokens = {p.owner_token for p in s.participants if p.owner_token}
        if not owner_tokens:
            out.append(s)
        elif token and token in owner_tokens:
            out.append(s)
    return out


def can_view_session(
    db: Session,
    session_id: str,
    token: str | None,
    *,
    allow_open_lobby: bool = True,
) -> bool:
    """判断请求方是否可以读取会话级资源。

    读取权限与 ``list_sessions_for_token`` 保持同一套口径：
    - 旧存档/纯本机会话没有任何 owner token，保持匿名可读；
    - 有归属的会话必须命中任一席位 owner token；
    - setup 阶段仍有空真人席时，允许访客读取大厅所需资源，认领后自动收紧。
    """
    session = db.get(GameSession, session_id)
    if session is None:
        return False

    participants = get_participants(db, session_id)
    owner_tokens = {p.owner_token for p in participants if p.owner_token}
    if not owner_tokens:
        return True
    if token and token in owner_tokens:
        return True
    if allow_open_lobby and session.status == "setup":
        return any(p.role == "human" and not p.claimed for p in participants)
    return False


def update_session_status(db: Session, session_id: str, status: str) -> GameSession | None:
    session = db.get(GameSession, session_id)
    if not session:
        return None
    session.status = status
    db.commit()
    db.refresh(session)
    return session


def get_session_events(
    db: Session, session_id: str, limit: int = 0, offset: int = 0
) -> list[EventLog]:
    """按 sequence_num 升序返回会话事件；默认 limit=0 即全量。

    默认必须是「全量」而非截断：本函数只服务于生成/上下文构建路径，它们要的是完整对话史
    （由 build_kp_context 的 token 预算 + 滚动摘要游标负责裁剪成实际喂给 LLM 的窗口）。
    早先默认 limit=100 会因升序取到「最早的 100 条」——会话过百条后 KP 上下文里全是旧事件、
    看不到最新玩家输入，导致跑团错乱。前端历史/重连分页走的是另一个 get_latest_events
    （带 before_seq），不受此默认影响。
    """
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
    """前端历史/重连分页用的最新事件页（升序返回）。

    「仅 KP 可见」事件（visibility 含 kp 哨兵，如幕后推演）在此过滤——本端点面向
    所有玩家，幕后事件永远不下发前端。过滤在取页之后做（幕后事件稀疏），某页可能
    略少于 limit，但 has_more/before_seq 分页语义不受影响。
    """
    q = db.query(EventLog).filter(EventLog.session_id == session_id)
    if before_seq is not None:
        q = q.filter(EventLog.sequence_num < before_seq)
    q = q.order_by(EventLog.sequence_num.desc())
    rows = q.limit(limit + 1).all()
    has_more = len(rows) > limit
    results = [e for e in rows[:limit] if not is_kp_only_event(e)]
    results.reverse()
    return results, has_more


def search_events(
    db: Session, session_id: str, query: str, limit: int = 30,
) -> list[EventLog]:
    """在本局历史里模糊检索（content LIKE），按时间倒序返回匹配的叙事/对话/行动/骰子/场外
    事件（排除系统提示等噪音）。空查询返回空列表。"""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = (
        db.query(EventLog)
        .filter(
            EventLog.session_id == session_id,
            EventLog.content.like(like),
            EventLog.event_type.in_(["narration", "dialogue", "action", "dice", "ooc"]),
        )
        .order_by(EventLog.sequence_num.desc())
        .limit(limit)
        .all()
    )
    # 双保险：幕后事件（event_type=system）本就被类型过滤挡住，这里再按 kp 哨兵
    # 显式过滤一次，防未来搜索范围扩大后泄露「仅 KP 可见」内容。
    return [e for e in rows if not is_kp_only_event(e)]


def human_character_ids(db: Session, session_id: str) -> set[str]:
    """本会话所有真人席位的角色 id（回合确认制里需要逐个确认推进的主体）。"""
    return {
        p.character_id
        for p in get_participants(db, session_id)
        if p.role == "human" and p.character_id
    }


def set_turn_confirm(db: Session, session_id: str, char_id: str, confirmed: bool) -> None:
    """记录/撤销某真人角色对『本回合推进』的确认（存 world_state.turn_confirm）。"""
    session = db.get(GameSession, session_id)
    if not session or not char_id:
        return
    ws = dict(session.world_state or {})
    tc = dict(ws.get("turn_confirm") or {})
    if confirmed:
        tc[char_id] = True
    else:
        tc.pop(char_id, None)
    ws["turn_confirm"] = tc
    session.world_state = ws
    db.commit()


def turn_confirm_state(
    db: Session, session_id: str, online_tokens: set[str] | None = None
) -> dict:
    """当前回合确认进度：{confirmed_ids, total, ready}。ready＝所有「需确认」真人都已确认。

    掉线豁免：给定 online_tokens 时，有归属但不在线的真人自动豁免——否则任一玩家关掉
    浏览器就会让整局永久卡死。无归属席位（纯本机会话）一律计入（无法判在线，按在场处理）。
    不给 online_tokens（旧调用/测试）时退化为「所有真人都需确认」的原行为。
    """
    session = db.get(GameSession, session_id)
    humans = [
        p for p in get_participants(db, session_id)
        if p.role == "human" and p.character_id
    ]
    if online_tokens is not None:
        humans = [
            p for p in humans
            if (not p.owner_token) or (p.owner_token in online_tokens)
        ]
    required_ids = {p.character_id for p in humans}
    tc = (session.world_state or {}).get("turn_confirm") if session else None
    tc = tc or {}
    confirmed = sorted(cid for cid in required_ids if tc.get(cid))
    total = len(required_ids)
    return {
        "confirmed_ids": confirmed,
        "total": total,
        "ready": total > 0 and len(confirmed) >= total,
    }


def commit_turn(db: Session, session_id: str) -> None:
    """推进：把本回合所有『暂存发言』(metadata.pending_turn) 转正（去标记），并清空确认状态。"""
    session = db.get(GameSession, session_id)
    if not session:
        return
    for ev in get_session_events(db, session_id, limit=0):
        meta = ev.metadata_ or {}
        if meta.get("pending_turn"):
            m = dict(meta)
            m.pop("pending_turn", None)
            ev.metadata_ = m
            flag_modified(ev, "metadata_")
    ws = dict(session.world_state or {})
    ws["turn_confirm"] = {}
    session.world_state = ws
    db.commit()


def delete_pending_event(db: Session, session_id: str, event_id: str, actor_id: str) -> bool:
    """删除一条『本回合暂存』发言：仅限本人、仅限 pending_turn（未推进）。返回是否删除。"""
    ev = db.get(EventLog, event_id)
    if not ev or ev.session_id != session_id:
        return False
    if not ev.actor_id or ev.actor_id != actor_id:
        return False
    if not (ev.metadata_ or {}).get("pending_turn"):
        return False
    db.delete(ev)
    db.commit()
    return True


def update_pending_event(
    db: Session, session_id: str, event_id: str, actor_id: str, content: str,
) -> bool:
    """改写一条『本回合暂存』发言的正文：仅限本人、仅限 pending_turn（未推进）。返回是否改写。"""
    ev = db.get(EventLog, event_id)
    if not ev or ev.session_id != session_id:
        return False
    if not ev.actor_id or ev.actor_id != actor_id:
        return False
    if not (ev.metadata_ or {}).get("pending_turn"):
        return False
    ev.content = content
    db.add(ev)
    db.commit()
    return True


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
    group: str | None = None,
) -> EventLog:
    meta = dict(metadata or {})
    # 分头行动：同一回合里不同分组/场景的内容，用 group 标签分栏渲染（KP 经 [GROUP] 标注）。
    if group:
        meta["group"] = group
    # 给事件打上「发生在哪个场景」的戳：NPC 上下文据此只看自己所在场景的事件，
    # 避免一个 NPC 知道玩家在别处发生的事（信息隔离）。调用方未显式给 scene_id 时取当前场景。
    if "scene_id" not in meta:
        sess = db.get(GameSession, session_id)
        if sess and sess.current_scene_id:
            meta["scene_id"] = sess.current_scene_id

    # sequence_num 由「读最大值 + 1」生成，多个请求并发时可能同时读到同一个值。
    # 唯一约束负责兜底，遇到撞号只回滚本次 INSERT 并重新取最大值；其它完整性错误原样抛出。
    for attempt in range(3):
        event = EventLog(
            session_id=session_id,
            sequence_num=get_next_sequence_num(db, session_id),
            event_type=event_type,
            actor_id=actor_id,
            actor_name=actor_name,
            content=content,
            visibility=visibility or [],
            metadata_=meta,
        )
        db.add(event)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            message = str(exc).lower()
            is_sequence_conflict = (
                "uq_event_logs_session_sequence" in message
                or "event_logs.session_id, event_logs.sequence_num" in message
            )
            if not is_sequence_conflict or attempt == 2:
                raise
            continue
        db.refresh(event)
        return event

    # 理论上第三次尝试会在 attempt == 2 时直接抛出；保留显式异常避免静态分析认为无返回。
    raise RuntimeError("事件序号分配失败")


def set_event_group(db: Session, event: EventLog, group: str) -> None:
    """给已落库的事件补打分组标签（分头行动：把本回合各角色行动归入其所在场景列）。"""
    meta = dict(event.metadata_ or {})
    if meta.get("group") == group:
        return
    meta["group"] = group
    event.metadata_ = meta
    flag_modified(event, "metadata_")  # JSON 列原地改字典不会被脏检测，需显式标记
    db.add(event)
    db.commit()


def can_manage_session(db: Session, session_id: str, token: str | None) -> bool:
    """房主管理权（结束会话、删除等破坏性/房主操作）：房主本人；或纯本机/旧会话
    （主角席无归属）时的本机用户。有主会话只允许房主，防同网段他人越权。"""
    seat = _primary_seat(db, session_id)
    if seat is None:
        return False
    if not seat.owner_token:  # 纯本机/旧会话，无归属 → 本机可管理（保持原体验）
        return True
    return bool(token and seat.owner_token == token)


def can_delete_session(db: Session, session_id: str, token: str | None) -> bool:
    """删除会话的鉴权，语义同 can_manage_session（房主或纯本机会话）。"""
    return can_manage_session(db, session_id, token)


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


# ── 按角色位置 / 已知地点（分头行动 + 大地图前往）──────────────────

def get_party_locations(session: GameSession) -> dict:
    """world_state.party_locations：{角色 id: 所在场景 id}。缺省时按需回落到当前场景。"""
    return dict((session.world_state or {}).get("party_locations") or {})


def get_char_location(session: GameSession, char_id: str | None) -> str | None:
    """某角色当前所在场景；无显式记录则回落到会话当前场景（向后兼容）。"""
    if not char_id:
        return session.current_scene_id
    return get_party_locations(session).get(char_id) or session.current_scene_id


def set_char_location(db: Session, session_id: str, char_id: str, scene_id: str) -> None:
    """把某角色移动到某场景（玩家经大地图前往 / AI 队友分头时的落点）。

    主角移动时一并更新 current_scene_id（地图面板、NPC 上下文等仍以它为锚）。目的地记入已访问。
    """
    if not (char_id and scene_id):
        return
    session = db.get(GameSession, session_id)
    if not session:
        return
    ws = dict(session.world_state or {})
    locs = dict(ws.get("party_locations") or {})
    locs[char_id] = scene_id
    ws["party_locations"] = locs
    visited = list(ws.get("visited_scenes") or [])
    if scene_id not in visited:
        visited.append(scene_id)
    ws["visited_scenes"] = visited
    session.world_state = ws
    if char_id == session.player_character_id:
        session.current_scene_id = scene_id
    db.commit()


# 地点名常见的「设施类型」后缀：按长度从长到短，供从场景标题析出可被对话提及的关键词。
_FACILITY_SUFFIXES = [
    "疗养院", "图书馆", "档案馆", "博物馆", "派出所", "警察局", "礼拜堂", "老房子",
    "报社", "医院", "教堂", "法院", "老宅", "宅邸", "公寓", "旅馆", "酒店",
    "饭店", "学校", "大学", "中学", "小学", "墓地", "墓园", "工厂", "仓库", "教会",
    "庄园", "别墅", "城堡", "监狱", "银行", "邮局", "车站", "码头", "农场", "矿场",
    "洞穴", "地窖", "街区", "房子", "宅", "街",
]


# 「状态修饰」后缀：跟在设施类型之后表示地点当下状态（沉思礼拜堂+废墟）。它们**只用于剥离**
# 得到核心地名，本身**不**作为解锁关键词（否则说个「废墟」就乱解锁）。
_MODIFIER_SUFFIXES = ["废墟", "遗址", "旧址", "遗迹", "残址", "废址", "旧宅"]


def derive_scene_keywords(title: str) -> set[str]:
    """从场景标题**确定性地**派生解锁关键词：完整标题 + 核心地名（剥离废墟/遗址等状态词）+
    设施类型后缀 + 专名前缀。玩家在对话/行动里提到其中任意一个即解锁该地点。

    例：
      「罗克斯伯里疗养院」→ {完整标题, "疗养院", "罗克斯伯里"}
      「沉思礼拜堂废墟」  → {完整标题, "沉思礼拜堂", "礼拜堂", "沉思"}
        （此前只得完整标题，故必须说全名才解锁——本函数补上核心名与专名）

    这是**兜底/派生**逻辑：新模组解析时会另外生成并存储更丰富的 keywords（含地址/俗称），
    运行时二者取并集（见 known_scene_ids）。
    """
    title = (title or "").strip()
    if not title:
        return set()
    keywords = {title}
    # 先剥掉结尾的状态修饰后缀（可叠多个）得到核心地名，核心名本身入库
    core = title
    changed = True
    while changed:
        changed = False
        for suf in _MODIFIER_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                core = core[: -len(suf)].strip("·的 ")
                changed = True
    if core != title and len(core) >= 2:
        keywords.add(core)
    # 对核心名跑设施后缀逻辑：加设施类型别名（礼拜堂）+ 最长后缀前的专名（沉思）
    matched = [suf for suf in _FACILITY_SUFFIXES if core.endswith(suf) and len(core) > len(suf)]
    keywords.update(matched)
    if matched:
        longest = max(matched, key=len)
        prefix = core[: -len(longest)].strip("·的 ")
        if len(prefix) >= 2:
            keywords.add(prefix)
    return {k for k in keywords if len(k) >= 2}


def scene_unlock_keywords(scene: dict) -> set[str]:
    """一个场景的全部解锁关键词 = 存储的 keywords（解析时生成，含地址/俗称）∪ 标题派生关键词。
    存储缺失（老模组）时退化为纯派生——这样『沉思礼拜堂废墟』说「沉思礼拜堂」也能解锁。"""
    stored = {
        k.strip() for k in (scene.get("keywords") or [])
        if isinstance(k, str) and len(k.strip()) >= 2
    }
    title = scene.get("title") or scene.get("name") or ""
    return stored | derive_scene_keywords(title)


def known_scene_ids(module, session: GameSession, events: list | None = None) -> set:
    """已知地点 = 已访问/当前所在 ∪ 对话中被提及过的场景（KP 或角色提到其名即解锁）。

    未访问、且对话从未提及的地点不在大地图上显示——避免直接剧透全图。
    """
    by_id = {s.get("id"): s for s in (module.scenes or []) if s.get("id")}
    known = set((session.world_state or {}).get("visited_scenes") or [])
    if session.current_scene_id:
        known.add(session.current_scene_id)
    convo = "\n".join(
        (getattr(e, "content", "") or "")
        for e in (events or [])
        if getattr(e, "event_type", None) in ("narration", "dialogue", "action", "system")
    )
    if convo:
        for sid, s in by_id.items():
            if sid in known:
                continue
            if any(kw in convo for kw in scene_unlock_keywords(s)):
                known.add(sid)
    return {sid for sid in known if sid in by_id}


def _scene_adjacency(module) -> dict[str, set[str]]:
    """场景连通图（``connections`` 的无向闭包）：作者单向填写也按双向通行。"""
    adj: dict[str, set[str]] = {}
    ids = {s.get("id") for s in (module.scenes or []) if s.get("id")}
    for s in (module.scenes or []):
        sid = s.get("id")
        if not sid:
            continue
        adj.setdefault(sid, set())
        for c in s.get("connections") or []:
            c = str(c or "").strip()
            if c and c != sid and c in ids:
                adj.setdefault(c, set())
                adj[sid].add(c)
                adj[c].add(sid)
    return adj


def scene_neighbors(module, scene_id: str | None) -> list[str]:
    """当前场景可直达的相邻场景 id（升序）。无图/无该场景返回空列表。"""
    if not scene_id:
        return []
    return sorted(_scene_adjacency(module).get(scene_id, ()))


def find_scene_path(module, start: str | None, dest: str) -> list[str] | None:
    """沿场景连通图找 ``start → dest`` 的最短路径（BFS，路径含两端点）。

    返回场景 id 列表；**确实不连通**返回 None（调用方据此拒绝切换）。
    保守把关——以下情形一律视为可直达（返回平凡路径，行为与没有连通图时一致）：
    - 模组任何场景都没填 connections（旧/手工模组，没建图，不能把它们走死）；
    - start 缺失（无当前位置可依据）；
    - start 或 dest 自身没有任何边（作者没为该地点建边，无拓扑可循）。
    """
    dest = (dest or "").strip()
    if not dest:
        return None
    if not start:
        return [dest]
    if start == dest:
        return [start]
    adj = _scene_adjacency(module)
    if not any(adj.values()):
        return [start, dest]
    if not adj.get(start) or not adj.get(dest):
        return [start, dest]
    seen = {start}
    queue: list[list[str]] = [[start]]
    while queue:
        path = queue.pop(0)
        for nxt in sorted(adj.get(path[-1], ())):
            if nxt in seen:
                continue
            if nxt == dest:
                return path + [nxt]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def list_known_locations(
    module, session: GameSession, char_id: str | None = None, events: list | None = None,
    char_names: dict[str, str] | None = None,
) -> list[dict]:
    """供「大地图/调查板」渲染：已知地点列表（当前所在、已访问、相互连接、队友分布）。

    - ``kind == "chapter"`` 的场景是叙事章节而非地点，不上图（当前正身处其中时除外）。
    - ``connections`` 只回已知集合内的邻居——未知地点绝不经边泄露。
    - ``char_names``（char_id → 名字）给定时，按 party_locations 归并各地点的在场成员。
    """
    by_id = {s.get("id"): s for s in (module.scenes or []) if s.get("id")}
    visited = set((session.world_state or {}).get("visited_scenes") or [])
    cur = get_char_location(session, char_id)
    shown = {
        sid for sid in known_scene_ids(module, session, events)
        if by_id[sid].get("kind") != "chapter" or sid == cur
    }
    # 队伍分布：各成员所在场景（party_locations 缺省回落主场景）
    party_at: dict[str, list[str]] = {}
    if char_names:
        pl = (session.world_state or {}).get("party_locations") or {}
        for cid, name in char_names.items():
            sid = pl.get(cid) or session.current_scene_id
            if sid:
                party_at.setdefault(sid, []).append(name)
    # 调查板红线：**已发现**的线索（clue_ledger）按其模组定义的 location 挂到地点上。
    # 只含玩家已触碰的线索——未发现的绝不上板（不剧透）。
    ledger = (session.world_state or {}).get("clue_ledger") or {}
    clue_by_id = {c.get("id"): c for c in (getattr(module, "clues", None) or []) if c.get("id")}
    clues_at: dict[str, list[dict]] = {}
    for cid, entry in ledger.items():
        cdef = clue_by_id.get(cid)
        loc = (cdef or {}).get("location")
        if cdef and loc:
            clues_at.setdefault(loc, []).append({
                "id": cid,
                "name": cdef.get("name") or cid,
                "status": (entry or {}).get("status") or "partial",
            })
    out = []
    for sid in shown:
        s = by_id[sid]
        conns = [c for c in (s.get("connections") or []) if c in shown and c != sid]
        out.append({
            "id": sid,
            "name": s.get("title") or s.get("name") or sid,
            "current": sid == cur,
            "visited": sid in visited,
            "connections": conns,
            "party": party_at.get(sid, []),
            "clues": clues_at.get(sid, []),
        })
    out.sort(key=lambda x: (not x["current"], not x["visited"], x["id"]))
    return out


def set_flag(db: Session, session_id: str, flag: str, value: bool = True) -> None:
    """置/清剧情标志（world_state.flags）。KP 通过 [SET_FLAG]/[CLEAR_FLAG] 推进剧情状态，
    场景/NPC 的状态变体据此切换。flag 名做轻量规范化（去空白），value=False 即清除该标志。"""
    flag = (flag or "").strip()
    if not flag:
        return
    session = db.get(GameSession, session_id)
    if not session:
        return
    ws = dict(session.world_state or {})
    flags = dict(ws.get("flags") or {})
    if value:
        flags[flag] = True
    else:
        flags.pop(flag, None)
    ws["flags"] = flags
    session.world_state = ws
    db.commit()
