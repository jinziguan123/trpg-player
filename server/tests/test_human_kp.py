"""真人 KP M1：席位授权、不开 AI 生成、工具动作复用确定性执行器。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, EventLog, GameSession, Module, SessionParticipant
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
