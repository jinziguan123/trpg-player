"""真人 KP M1：席位授权、不开 AI 生成、工具动作复用确定性执行器。"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, EventLog, Module
from app.api.chat import kp_action
from app.schemas.session import KpActionRequest
from app.services import session_service
from app.services.chat_service import execute_human_kp_action, initialize_human_session
from app.services.room_hub import room_hub


def _db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'human-kp.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="真人 KP 测试", description="一场真人 KP 测试。", rule_system="coc",
        scenes=[{"id": "s1", "title": "门厅"}],
        npcs=[{"id": "guard", "name": "守门人", "skills": {"潜行": 55, "力量": 50}}],
    )
    hero = Character(
        name="调查员", rule_system="coc", is_player=True,
        skills={"侦查": 60, "力量": 65},
    )
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


def test_human_kp_skill_check_selects_npc_and_applies_bonus_die(tmp_path):
    db = _db(tmp_path)()
    module, _hero, session = _seed(db)

    chunks, result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "dice_check",
        {"skill": "潜行", "char": "npc:guard", "bonus": "1"},
    ))

    payload = json.loads(chunks[0].splitlines()[0][6:])
    detail = payload["metadata"]["dice"]
    assert payload["metadata"]["actor"] == "守门人"
    assert detail["bonus"] == 1 and detail["penalty"] == 0
    assert len(detail["tens"]) == 2
    assert "守门人" in result


def test_human_kp_blind_skill_check_returns_private_result_without_persisting_it(tmp_path):
    db = _db(tmp_path)()
    module, _hero, session = _seed(db)

    _chunks, result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "dice_check",
        {"skill": "潜行", "char": "npc:guard", "visibility": "blind"},
    ))

    event = db.query(EventLog).filter(EventLog.session_id == session.id).one()
    assert event.metadata_["blind"] is True
    assert "roll" not in event.metadata_ and "outcome" not in event.metadata_
    assert "结果仅 KP 可见" in event.content
    assert "达成" in result


def test_human_kp_generic_roll_supports_pool_and_blind_result(tmp_path):
    db = _db(tmp_path)()
    module, _hero, session = _seed(db)

    chunks, open_result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "generic_roll",
        {"count": 2, "sides": 8, "modifier": 3, "reason": "参战敌人数"},
    ))
    payload = json.loads(chunks[0].splitlines()[0][6:])
    assert payload["metadata"]["dice"]["kind"] == "pool"
    assert payload["metadata"]["dice"]["notation"] == "2d8+3"
    assert len(payload["metadata"]["dice"]["dice"]) == 2
    assert "参战敌人数" in open_result and "=" in open_result

    _chunks, blind_result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "generic_roll",
        {"count": 1, "sides": 20, "reason": "NPC 是否理解画外音", "visibility": "blind"},
    ))
    blind_event = (
        db.query(EventLog)
        .filter(EventLog.session_id == session.id)
        .order_by(EventLog.sequence_num.desc())
        .first()
    )
    assert blind_event.metadata_["blind"] is True
    assert "dice" not in blind_event.metadata_ and "total" not in blind_event.metadata_
    assert "结果仅 KP 可见" in blind_event.content
    assert "=" in blind_result

    with pytest.raises(ValueError, match="1 到 20"):
        asyncio.run(execute_human_kp_action(
            db, session.id, session, module, "generic_roll",
            {"count": 21, "sides": 6},
        ))


def test_human_kp_action_broadcast_never_contains_blind_result(tmp_path, monkeypatch):
    db = _db(tmp_path)()
    _module, _hero, session = _seed(db)
    broadcasts: list[str] = []
    monkeypatch.setattr(room_hub, "broadcast", lambda _sid, chunk: broadcasts.append(chunk))

    response = asyncio.run(kp_action(
        session.id,
        KpActionRequest(
            action="generic_roll",
            payload={"count": 2, "sides": 6, "reason": "暗中决定人数", "visibility": "blind"},
        ),
        db,
        "kp-token",
    ))

    assert "=" in response["result"]
    public_stream = "".join(broadcasts)
    assert response["result"] not in public_stream
    assert "结果仅 KP 可见" in public_stream


def test_human_kp_opposed_check_rejects_self_and_emits_frontend_contract(tmp_path):
    db = _db(tmp_path)()
    module, hero, session = _seed(db)
    hero_ref = f"character:{hero.id}"

    with pytest.raises(ValueError, match="不能是同一个对象"):
        asyncio.run(execute_human_kp_action(
            db, session.id, session, module, "opposed_check",
            {"a": hero_ref, "a_skill": "力量", "b": hero_ref, "b_skill": "力量"},
        ))

    chunks, _result = asyncio.run(execute_human_kp_action(
        db, session.id, session, module, "opposed_check",
        {
            "a": hero_ref, "a_skill": "力量", "a_bonus": "1",
            "b": "npc:guard", "b_skill": "力量", "b_penalty": "1",
        },
    ))
    payload = json.loads(chunks[0].splitlines()[0][6:])
    opposed = payload["metadata"]["opposed"]
    assert opposed["attacker"]["name"] == "调查员"
    assert opposed["defender"]["name"] == "守门人"
    assert opposed["winner"] in ("attacker", "defender", None)
    assert payload["metadata"]["a"]["dice"]["bonus"] == 1
    assert payload["metadata"]["b"]["dice"]["penalty"] == 1


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
