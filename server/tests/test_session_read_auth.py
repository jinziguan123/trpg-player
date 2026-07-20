"""会话级读取授权的统一回归测试。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Character, GameSession, Module, SessionParticipant
from app.services import session_service


@pytest.fixture
def auth_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'session-read-auth.db'}",
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
        title="读取授权测试",
        rule_system="coc",
        scenes=[{"id": "hall", "title": "门厅"}],
        npcs=[],
    )
    hero = Character(name="房主角色", rule_system="coc", is_player=True)
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
        )
    )
    db.commit()
    session_service.add_event(db, session.id, "action", "我查看门厅", actor_name=hero.name)
    ids = {"session": session.id, "hero": hero.id, "module": module.id}
    db.close()

    yield TestClient(app), ids, testing_session
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/{sid}",
        "/api/sessions/{sid}/context-estimate",
        "/api/sessions/{sid}/rag-stats",
        "/api/sessions/{sid}/recaps",
        "/api/sessions/{sid}/growth?character_id={hero}",
        "/api/sessions/{sid}/improvised-npcs",
        "/api/sessions/{sid}/replay",
        "/api/sessions/{sid}/events",
        "/api/sessions/{sid}/generating",
        "/api/sessions/{sid}/search?q=门厅",
        "/api/sessions/{sid}/locations",
        "/api/sessions/{sid}/combat",
        "/api/sessions/{sid}/chase",
        "/api/sessions/{sid}/inventory?char_id={hero}",
        "/api/sessions/{sid}/live",
    ],
)
def test_owned_session_reads_reject_strangers(auth_env, path):
    client, ids, _ = auth_env
    url = path.format(sid=ids["session"], hero=ids["hero"])

    assert client.get(url).status_code == 403
    assert client.get(url, headers={"X-Player-Token": "stranger"}).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/{sid}",
        "/api/sessions/{sid}/rag-stats",
        "/api/sessions/{sid}/recaps",
        "/api/sessions/{sid}/growth?character_id={hero}",
        "/api/sessions/{sid}/improvised-npcs",
        "/api/sessions/{sid}/events",
        "/api/sessions/{sid}/generating",
        "/api/sessions/{sid}/search?q=门厅",
        "/api/sessions/{sid}/locations",
        "/api/sessions/{sid}/combat",
        "/api/sessions/{sid}/chase",
        "/api/sessions/{sid}/inventory?char_id={hero}",
    ],
)
def test_owned_session_reads_allow_member(auth_env, path):
    client, ids, _ = auth_env
    url = path.format(sid=ids["session"], hero=ids["hero"])

    response = client.get(url, headers={"X-Player-Token": "host-token"})
    assert response.status_code == 200, response.text


def test_open_lobby_is_readable_until_last_seat_is_claimed(auth_env):
    _, ids, testing_session = auth_env
    db = testing_session()
    try:
        session = db.get(GameSession, ids["session"])
        session.status = "setup"
        db.add(
            SessionParticipant(
                session_id=session.id,
                character_id=None,
                role="human",
                is_primary=False,
                owner_token=None,
                claimed=False,
                ready=False,
                seat_order=1,
            )
        )
        db.commit()

        assert session_service.can_view_session(db, session.id, "guest-token") is True

        seat = session_service.get_participants(db, session.id)[1]
        seat.character_id = ids["hero"]
        seat.owner_token = "guest-token"
        seat.claimed = True
        db.commit()

        assert session_service.can_view_session(db, session.id, "guest-token") is True
        assert session_service.can_view_session(db, session.id, "stranger") is False
    finally:
        db.close()


def test_legacy_unowned_session_remains_anonymously_readable(auth_env):
    _, ids, testing_session = auth_env
    db = testing_session()
    try:
        participant = session_service.get_participants(db, ids["session"])[0]
        participant.owner_token = None
        db.commit()

        assert session_service.can_view_session(db, ids["session"], None) is True
    finally:
        db.close()
