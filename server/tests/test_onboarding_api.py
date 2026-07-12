from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import ai_settings
from app.database import get_db
from app.main import app
from app.models import Base


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'onboarding-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(
        ai_settings,
        "load_active_profile",
        lambda: SimpleNamespace(api_key="test-key", model_name="test-model"),
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_start_requires_player_token(client):
    response = client.post("/api/onboarding/start")

    assert response.status_code == 401


def test_start_requires_active_ai_profile(client, monkeypatch):
    monkeypatch.setattr(ai_settings, "load_active_profile", lambda: None)

    response = client.post(
        "/api/onboarding/start",
        headers={"X-Player-Token": "player-a"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ai_not_configured"


def test_start_returns_created_session(client):
    response = client.post(
        "/api/onboarding/start",
        headers={"X-Player-Token": "player-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["reused"] is False
    assert response.json()["session_id"]


def test_start_reuses_session_for_same_player(client):
    headers = {"X-Player-Token": "player-a"}

    first = client.post("/api/onboarding/start", headers=headers)
    second = client.post("/api/onboarding/start", headers=headers)

    assert second.status_code == 200
    assert second.json()["session_id"] == first.json()["session_id"]
    assert second.json()["reused"] is True
