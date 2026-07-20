"""阶段 2b：房间码、空席、认领、角色归属。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (  # noqa: F401
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rooms.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="房主角色", rule_system="coc", is_player=True)
    guest = Character(name="访客角色", rule_system="coc", is_player=True)
    db.add_all([module, hero, guest])
    db.commit()
    return module, hero, guest


def test_create_room_with_empty_seat(db_factory):
    db = db_factory()
    module, hero, guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "role": "human", "is_primary": True},
            {"character_id": None, "role": "human"},  # 空席待加入
        ],
        creator_token="host-token",
    )
    assert session.room_code and len(session.room_code) >= 6
    parts = session_service.get_participants(db, session.id)
    primary = next(p for p in parts if p.is_primary)
    empty = next(p for p in parts if not p.is_primary)
    assert primary.owner_token == "host-token" and primary.claimed
    assert empty.character_id is None and empty.claimed is False
    # 房主角色绑定到房主 token
    db.refresh(hero)
    assert hero.owner_token == "host-token"


def test_get_session_by_code(db_factory):
    db = db_factory()
    module, hero, _ = _seed(db)
    session = session_service.create_session(
        db, module.id, [{"character_id": hero.id, "is_primary": True}],
        creator_token="host",
    )
    found = session_service.get_session_by_code(db, session.room_code.lower())
    assert found and found.id == session.id


def test_claim_seat(db_factory):
    db = db_factory()
    module, hero, guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="host",
    )
    empty = next(p for p in session_service.get_participants(db, session.id) if not p.is_primary)

    session_service.claim_seat(db, session.id, empty.seat_order, guest.id, "guest-token")

    seat = next(p for p in session_service.get_participants(db, session.id) if p.seat_order == empty.seat_order)
    assert seat.claimed and seat.character_id == guest.id and seat.owner_token == "guest-token"
    db.refresh(guest)
    assert guest.owner_token == "guest-token"


def test_join_reserves_seat_and_lists_room_for_token(db_factory):
    db = db_factory()
    module, hero, _guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "role": "human", "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="host",
    )

    session_service.join_session(db, session.id, "guest-token")

    reserved = next(
        p for p in session_service.get_participants(db, session.id)
        if p.owner_token == "guest-token"
    )
    assert reserved.character_id is None and reserved.claimed and not reserved.ready
    assert session.id in {s.id for s in session_service.list_sessions_for_token(db, "guest-token")}
    # 重复进入幂等，不会再占第二个席位。
    session_service.join_session(db, session.id, "guest-token")
    assert len([
        p for p in session_service.get_participants(db, session.id)
        if p.owner_token == "guest-token"
    ]) == 1


def test_reserved_seat_can_later_select_existing_character(db_factory):
    db = db_factory()
    module, hero, guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "role": "human", "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="host",
    )
    session_service.join_session(db, session.id, "guest-token")
    reserved = next(
        p for p in session_service.get_participants(db, session.id)
        if p.owner_token == "guest-token"
    )

    session_service.claim_seat(db, session.id, reserved.seat_order, guest.id, "guest-token")

    assigned = next(
        p for p in session_service.get_participants(db, session.id)
        if p.seat_order == reserved.seat_order
    )
    assert assigned.character_id == guest.id and assigned.owner_token == "guest-token"


def test_claim_rejects_taken_and_wrong_owner(db_factory):
    db = db_factory()
    module, hero, guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="host",
    )
    seat_order = next(p.seat_order for p in session_service.get_participants(db, session.id) if not p.is_primary)

    # 认领他人 token 拥有的角色应被拒
    guest.owner_token = "someone-else"
    db.commit()
    with pytest.raises(ValueError):
        session_service.claim_seat(db, session.id, seat_order, guest.id, "guest-token")

    # 认领已被占用的主角席应被拒
    with pytest.raises(ValueError):
        session_service.claim_seat(db, session.id, 0, guest.id, "guest-token")


def test_resolve_actor_token_ownership(db_factory):
    db = db_factory()
    module, hero, guest = _seed(db)
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="host",
    )
    seat_order = next(p.seat_order for p in session_service.get_participants(db, session.id) if not p.is_primary)
    session_service.claim_seat(db, session.id, seat_order, guest.id, "guest")

    # 房主以自己角色行动 OK
    assert session_service.resolve_actor(db, session.id, "host", hero.id).id == hero.id
    # 访客以自己角色行动 OK
    assert session_service.resolve_actor(db, session.id, "guest", guest.id).id == guest.id
    # 访客冒用房主角色行动 → 拒
    with pytest.raises(ValueError):
        session_service.resolve_actor(db, session.id, "guest", hero.id)
    # 不传 acting_character_id 默认主角
    assert session_service.resolve_actor(db, session.id, "host", None).id == hero.id
