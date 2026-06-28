"""模组手动新建/编辑/查看 API 回归。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Module  # noqa: F401 注册表


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _payload(title="失踪的考古队"):
    return {
        "title": title,
        "rule_system": "coc",
        "description": "一句话简介",
        "world_setting": {"era": "1920s", "location": "埃及", "player_brief": "你受托调查"},
        "scenes": [{"id": "s1", "name": "入口", "description": "墓门", "connections": []}],
        "npcs": [{"id": "n1", "name": "向导", "secrets": ["知道密道"], "skills": {"侦查": 50}}],
        "clues": [{"id": "c1", "name": "笔记", "location": "s1", "trigger_condition": "搜索"}],
    }


def test_create_get_update_module(client):
    # 新建
    r = client.post("/api/modules", json=_payload())
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert r.json()["world_setting"]["player_brief"] == "你受托调查"

    # 查看（含完整结构化内容）
    g = client.get(f"/api/modules/{mid}").json()
    assert g["scenes"][0]["name"] == "入口"
    assert g["npcs"][0]["secrets"] == ["知道密道"]
    assert g["clues"][0]["trigger_condition"] == "搜索"

    # 编辑：改标题、加一个场景
    upd = _payload(title="改名后的模组")
    upd["scenes"].append({"id": "s2", "name": "墓室", "description": "深处", "connections": []})
    u = client.put(f"/api/modules/{mid}", json=upd)
    assert u.status_code == 200
    assert u.json()["title"] == "改名后的模组"
    assert len(u.json()["scenes"]) == 2


def test_create_rejects_empty_title(client):
    p = _payload(title="   ")
    assert client.post("/api/modules", json=p).status_code == 400


def test_update_missing_404(client):
    assert client.put("/api/modules/nope", json=_payload()).status_code == 404
