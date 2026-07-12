import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module, SessionParticipant


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'onboarding.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_start_creates_owned_sample_module_character_and_active_session(db):
    from app.services.onboarding_service import start_onboarding

    game, reused = start_onboarding(db, "player-a")

    assert reused is False
    assert game.status == "active"
    module = db.get(Module, game.module_id)
    assert module is not None
    assert module.world_setting["source"] == "trpg-player-original"
    assert module.world_setting["sample_slug"] == "first-case-v1"
    assert len(module.scenes) >= 2

    character = db.get(Character, game.player_character_id)
    assert character is not None
    assert character.owner_token == "player-a"
    assert character.module_id == module.id

    seats = db.query(SessionParticipant).filter_by(session_id=game.id).all()
    assert len(seats) == 1
    assert seats[0].role == "human"
    assert seats[0].is_primary is True
    assert seats[0].ready is True
    assert seats[0].owner_token == "player-a"


def test_start_reuses_active_onboarding_session_for_same_token(db):
    from app.services.onboarding_service import start_onboarding

    first, first_reused = start_onboarding(db, "player-a")
    second, second_reused = start_onboarding(db, "player-a")

    assert first_reused is False
    assert second_reused is True
    assert second.id == first.id
    assert db.query(GameSession).count() == 1
    assert db.query(Character).count() == 1
    assert db.query(Module).count() == 1


def test_different_tokens_do_not_share_player_character(db):
    from app.services.onboarding_service import start_onboarding

    first, _ = start_onboarding(db, "player-a")
    second, _ = start_onboarding(db, "player-b")

    assert first.id != second.id
    assert first.player_character_id != second.player_character_id
    assert db.query(Module).count() == 1
    assert db.query(Character).count() == 2


def test_start_rolls_back_everything_when_session_creation_fails(db, monkeypatch):
    from app.services import onboarding_service

    def fail_create(*_args, **_kwargs):
        raise ValueError("模拟会话创建失败")

    monkeypatch.setattr(onboarding_service.session_service, "create_session", fail_create)

    with pytest.raises(ValueError, match="模拟会话创建失败"):
        onboarding_service.start_onboarding(db, "player-a")

    assert db.query(Module).count() == 0
    assert db.query(Character).count() == 0
    assert db.query(GameSession).count() == 0
