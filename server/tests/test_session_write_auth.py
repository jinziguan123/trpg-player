"""会话写授权的统一回归测试。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Character, GameSession, Module, SessionParticipant
from app.services import session_service


@pytest.fixture
def write_auth_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'session-write-auth.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    db = testing_session()
    module = Module(
        title="写授权测试",
        rule_system="coc",
        scenes=[{"id": "hall", "title": "门厅"}],
        npcs=[],
    )
    hero = Character(
        name="房主角色",
        rule_system="coc",
        is_player=True,
        skills={"侦查": 50},
    )
    db.add_all([module, hero])
    db.flush()
    session = GameSession(
        module_id=module.id,
        player_character_id=hero.id,
        current_scene_id="hall",
        status="active",
        world_state={"visited_scenes": ["hall"]},
    )
    db.add(session)
    db.flush()
    db.add(
        SessionParticipant(
            session_id=session.id,
            character_id=hero.id,
            role="human",
            is_primary=True,
            owner_token="host-token",
            claimed=True,
            ready=True,
            seat_order=0,
        )
    )
    db.commit()
    ids = {"session": session.id, "hero": hero.id}
    db.close()

    yield TestClient(app), ids, testing_session
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", "/api/sessions/{sid}/growth/settle", {"json": {"character_id": "unknown"}}),
        ("post", "/api/sessions/{sid}/opening", {}),
        ("post", "/api/sessions/{sid}/recap", {}),
        ("post", "/api/sessions/{sid}/typing", {}),
        ("post", "/api/sessions/{sid}/ready", {"json": {"ready": True}}),
        ("post", "/api/sessions/{sid}/start", {}),
        ("post", "/api/sessions/{sid}/kick/1", {}),
        ("post", "/api/sessions/{sid}/improvised-npcs/promote", {"json": {"name": "路人"}}),
        ("post", "/api/sessions/{sid}/end-vote", {}),
        ("delete", "/api/sessions/{sid}/end-vote", {}),
        ("post", "/api/sessions/{sid}/roll", {"json": {"check_id": "missing"}}),
        ("post", "/api/sessions/{sid}/regenerate", {}),
        ("post", "/api/sessions/{sid}/combat/action", {"json": {"type": "attack"}}),
        ("post", "/api/sessions/{sid}/chase/action", {"json": {"type": "run"}}),
    ],
)
def test_owned_session_writes_reject_strangers(write_auth_env, method, path, kwargs):
    client, ids, _ = write_auth_env
    url = path.format(sid=ids["session"], hero=ids["hero"])
    response = getattr(client, method)(url, headers={"X-Player-Token": "stranger"}, **kwargs)
    assert response.status_code == 403, response.text


def test_owned_session_combat_does_not_fall_back_to_primary_for_stranger(write_auth_env):
    client, ids, _ = write_auth_env

    response = client.post(
        f"/api/sessions/{ids['session']}/combat/action",
        json={"type": "attack"},
    )

    assert response.status_code == 403, response.text


def test_member_can_settle_own_growth(write_auth_env):
    client, ids, _ = write_auth_env

    response = client.post(
        f"/api/sessions/{ids['session']}/growth/settle",
        headers={"X-Player-Token": "host-token"},
        json={"character_id": ids["hero"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["character_id"] == ids["hero"]


def test_member_cannot_submit_another_players_pending_check(write_auth_env):
    client, ids, testing_session = write_auth_env
    db = testing_session()
    other = Character(name="其他玩家", rule_system="coc", is_player=True)
    db.add(other)
    db.flush()
    db.add(
        SessionParticipant(
            session_id=ids["session"],
            character_id=other.id,
            role="human",
            is_primary=False,
            owner_token="other-token",
            claimed=True,
            ready=True,
            seat_order=1,
        )
    )
    session = db.get(GameSession, ids["session"])
    session.world_state = {
        "pending_checks": {
            "other-check": {
                "id": "other-check",
                "char_id": other.id,
                "skill": "侦查",
            }
        }
    }
    db.commit()
    db.close()

    response = client.post(
        f"/api/sessions/{ids['session']}/roll",
        headers={"X-Player-Token": "host-token"},
        json={"check_id": "other-check"},
    )

    assert response.status_code == 403, response.text


def test_token_actor_keeps_legacy_unowned_fallback(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'token-actor.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = testing_session()
    module = Module(title="旧会话", rule_system="coc", scenes=[], npcs=[])
    hero = Character(name="旧主角", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.flush()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session)
    db.flush()
    db.add(
        SessionParticipant(
            session_id=session.id,
            character_id=hero.id,
            role="human",
            is_primary=True,
            claimed=True,
            seat_order=0,
        )
    )
    db.commit()

    assert session_service.resolve_token_actor(db, session.id, None).id == hero.id
    db.close()
