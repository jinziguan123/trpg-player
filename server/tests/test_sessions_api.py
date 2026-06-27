"""会话 / 角色 API 的 HTTP 层回归（含多席位序列化与 available 过滤）。

用 TestClient + get_db 依赖覆盖，临时 SQLite，不触达真实 LLM。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import (  # noqa: F401 — 注册全部表
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSession()
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc", is_player=True)
    ally = Character(name="AI队友", rule_system="coc", is_player=False)
    db.add_all([module, hero, ally])
    db.commit()
    ids = {"module": module.id, "hero": hero.id, "ally": ally.id}
    db.close()

    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_create_session_with_participants_roundtrip(client):
    c, ids = client
    resp = c.post(
        "/api/sessions",
        json={
            "module_id": ids["module"],
            "participants": [
                {"character_id": ids["hero"], "role": "human", "is_primary": True},
                {"character_id": ids["ally"], "role": "ai", "is_primary": False},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_character_id"] == ids["hero"]
    parts = {p["character_id"]: p for p in body["participants"]}
    assert parts[ids["hero"]]["role"] == "human" and parts[ids["hero"]]["is_primary"]
    assert parts[ids["ally"]]["role"] == "ai"
    assert parts[ids["ally"]]["character_name"] == "AI队友"

    # get_session 也带参与者与名字
    sid = body["id"]
    got = c.get(f"/api/sessions/{sid}").json()
    assert len(got["participants"]) == 2
    assert any(p["character_name"] == "主角" for p in got["participants"])


def test_available_filter_excludes_occupied_and_respects_is_player(client):
    c, ids = client
    # 开局占用 hero + ally
    c.post(
        "/api/sessions",
        json={
            "module_id": ids["module"],
            "participants": [
                {"character_id": ids["hero"], "is_primary": True},
                {"character_id": ids["ally"], "role": "ai"},
            ],
        },
    )
    # available 的主角池应排除已占用的 hero
    heroes = c.get("/api/characters?available=true&is_player=true").json()
    assert ids["hero"] not in {h["id"] for h in heroes}
    # available 的队友池应排除已占用的 ally（参与表对齐）
    allies = c.get("/api/characters?available=true&is_player=false").json()
    assert ids["ally"] not in {a["id"] for a in allies}


def test_legacy_single_player_create_still_works(client):
    c, ids = client
    resp = c.post(
        "/api/sessions",
        json={"module_id": ids["module"], "player_character_id": ids["hero"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_character_id"] == ids["hero"]
    assert len(body["participants"]) == 1
    assert body["participants"][0]["is_primary"]
