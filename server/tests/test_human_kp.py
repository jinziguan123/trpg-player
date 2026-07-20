"""真人 KP M1：席位授权、不开 AI 生成、工具动作复用确定性执行器。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, EventLog, Module
from app.services import session_service
from app.services.chat_service import execute_human_kp_action, initialize_human_session


def _db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'human-kp.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="真人 KP 测试", description="一场真人 KP 测试。", rule_system="coc", scenes=[{"id": "s1", "title": "门厅"}],
    )
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = session_service.create_session(
        db, module.id, [{"character_id": hero.id, "role": "human", "is_primary": True}],
        creator_token="kp-token", kp_mode="human",
    )
    return module, hero, session


def test_human_kp_creates_separate_owned_seat_and_authorizes(tmp_path):
    db = _db(tmp_path)()
    _module, hero, session = _seed(db)
    parts = session_service.get_participants(db, session.id)
    kp = next(p for p in parts if p.role == "kp")
    assert kp.character_id is None and kp.owner_token == "kp-token"
    assert session_service.is_kp(db, session.id, "kp-token")
    assert not session_service.is_kp(db, session.id, "player-token")
    assert session_service.authorize_kp(db, session.id, "kp-token").id == session.id
    assert session_service.resolve_actor(db, session.id, "kp-token", hero.id).id == hero.id


def test_human_kp_token_uses_player_seat_for_ready_state(tmp_path):
    db = _db(tmp_path)()
    _module, _hero, session = _seed(db)
    parts = session_service.get_participants(db, session.id)
    kp = next(p for p in parts if p.role == "kp")
    player = next(p for p in parts if p.role == "human")
    assert kp.owner_token == player.owner_token == "kp-token"
    assert kp.ready is True

    session_service.set_ready(db, session.id, "kp-token", False)
    db.refresh(kp)
    db.refresh(player)
    assert kp.ready is True
    assert player.ready is False


def test_human_kp_action_publishes_narration_without_llm(tmp_path):
    db = _db(tmp_path)()
    module, _hero, session = _seed(db)
    chunks, result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "narration", {"content": "门厅的灯突然熄灭。"},
    ))
    assert "已发布" in result
    assert len(chunks) == 1
    event = db.query(EventLog).filter(EventLog.session_id == session.id).one()
    assert event.event_type == "narration"
    assert event.actor_name == "KP"
    assert event.metadata_["kp_manual"] is True


def test_human_kp_action_uses_seated_player_when_primary_is_empty(tmp_path):
    db = _db(tmp_path)()
    module = Module(title="真人 KP 空主角席", rule_system="coc", scenes=[])
    hero = Character(name="临时调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [{"character_id": None, "role": "human", "is_primary": True}],
        creator_token="kp-token", kp_mode="human",
    )
    player_seat = next(
        p for p in session_service.get_participants(db, session.id)
        if p.role == "human"
    )
    session_service.claim_seat(
        db, session.id, player_seat.seat_order, hero.id, "player-token",
    )
    db.refresh(session)

    chunks, result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "narration", {"content": "走廊尽头传来脚步声。"},
    ))

    assert chunks and "已发布" in result


def test_human_kp_open_player_seat_can_start_without_counting_kp_seat(tmp_path):
    db_factory = _db(tmp_path)
    db = db_factory()
    module = Module(title="真人 KP 大厅", rule_system="coc", scenes=[])
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    guest = Character(name="队友", rule_system="coc", is_player=True)
    db.add_all([module, hero, guest])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "role": "human", "is_primary": True},
            {"character_id": None, "role": "human"},
        ],
        creator_token="kp-token", kp_mode="human",
    )
    empty = [p for p in session_service.get_participants(db, session.id) if p.role != "kp" and not p.character_id]
    assert len(empty) == 1
    with pytest.raises(ValueError, match="空席"):
        session_service.start_game(db, session.id, "kp-token")
    session_service.claim_seat(db, session.id, empty[0].seat_order, guest.id, "guest-token")
    session_service.set_ready(db, session.id, "guest-token", True)
    assert session_service.start_game(db, session.id, "kp-token").status == "active"


def test_new_human_kp_creator_only_owns_kp_and_token_cannot_claim_player(tmp_path):
    db_factory = _db(tmp_path)
    db = db_factory()
    module = Module(title="严格身份模型", rule_system="coc", scenes=[])
    guest = Character(name="玩家", rule_system="coc", is_player=True)
    db.add_all([module, guest])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [{"character_id": None, "role": "human", "is_primary": True}],
        creator_token="kp-token", kp_mode="human",
    )
    parts = session_service.get_participants(db, session.id)
    player = next(p for p in parts if p.role == "human")
    kp = next(p for p in parts if p.role == "kp")
    assert session.identity_version == 2
    assert session.host_token == "kp-token"
    assert player.owner_token is None and not player.claimed and not player.ready
    assert kp.owner_token == "kp-token" and kp.claimed and kp.ready
    assert session_service.is_host(db, session.id, "kp-token")
    assert not session_service.is_host(db, session.id, "player-token")

    with pytest.raises(ValueError, match="只能占用一个席位"):
        session_service.claim_seat(db, session.id, player.seat_order, guest.id, "kp-token")

    session_service.claim_seat(db, session.id, player.seat_order, guest.id, "player-token")
    session_service.set_ready(db, session.id, "player-token", True)
    assert session_service.start_game(db, session.id, "kp-token").status == "active"


def test_kp_seat_can_be_claimed_without_character(tmp_path):
    db_factory = _db(tmp_path)
    db = db_factory()
    module = Module(title="KP 认领", rule_system="coc", scenes=[])
    db.add(module)
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [{"character_id": None, "role": "human", "is_primary": True}],
        creator_token="owner-token", kp_mode="human",
    )
    kp = next(p for p in session_service.get_participants(db, session.id) if p.role == "kp")
    kp.owner_token = None
    kp.claimed = False
    session.host_token = None
    db.commit()

    session_service.claim_seat(db, session.id, kp.seat_order, None, "new-kp-token")
    claimed = next(p for p in session_service.get_participants(db, session.id) if p.role == "kp")
    assert claimed.claimed and claimed.ready and claimed.owner_token == "new-kp-token"


def test_human_opening_only_initializes_public_cards(tmp_path, monkeypatch):
    db_factory = _db(tmp_path)
    db = db_factory()
    module, _hero, session = _seed(db)
    import app.database as database
    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr("app.services.chat_service.room_hub.broadcast", lambda *_a, **_k: None)
    asyncio.run(initialize_human_session(session.id))
    fresh = db_factory()
    events = session_service.get_session_events(fresh, session.id)
    assert any((e.metadata_ or {}).get("kind") == "module_intro" for e in events)
    assert not any(e.event_type == "narration" for e in events)
