"""规则书 API 的 HTTP 层回归（上传→后台入库→列表→检索→删除）。

不触达真实嵌入模型：把 rulebook_service 的 get_embedder/extract_pages 打桩，
并把 app.database.SessionLocal 指向测试库（后台任务自开会话）。TestClient 会在
响应后同步跑完 BackgroundTasks，故 upload 返回后即可断言 ready。
"""

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.ai.embedding import Embedder
from app.database import get_db
from app.main import app
from app.models import Base, RuleChunk, Rulebook  # noqa: F401 注册表
from app.services import rulebook_service


class FakeEmbedder(Embedder):
    model_name = "fake-api"
    dim = 64

    def _vec(self, t):
        v = np.zeros(self.dim, dtype=np.float32)
        for ch in t:
            v[ord(ch) % self.dim] += 1.0
        return v.tolist()

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


CANNED = [
    (1, "技能检定流程：投掷 d100，结果小于等于技能值即为普通成功，难度由守秘人选定。"),
    (2, "孤注一掷：技能检定失败后可加倍投入重新掷骰一次，若再次失败则后果显著加重。"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    # 后台入库任务自开 SessionLocal —— 指向测试库，并打桩嵌入/抽取
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    monkeypatch.setattr(rulebook_service, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(rulebook_service, "extract_pages", lambda b: CANNED)

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_upload_then_indexed_and_searchable(client):
    resp = client.post(
        "/api/rulebooks/upload",
        files={"file": ("coc.pdf", b"%PDF-1.4 fake", "application/pdf")},
        params={"title": "守秘人规则书", "rule_system": "coc"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "守秘人规则书"
    # 响应在后台任务前生成 → 仍是 indexing
    assert body["status"] == "indexing"

    # TestClient 已同步跑完后台入库 → 列表应为 ready
    lst = client.get("/api/rulebooks").json()
    assert len(lst) == 1
    assert lst[0]["status"] == "ready"
    assert lst[0]["chunk_count"] == 2
    assert lst[0]["embed_model"] == "fake-api"

    # 检索命中孤注一掷那块
    hits = client.get(
        "/api/rulebooks/search", params={"q": "孤注一掷 重掷", "rule_system": "coc"}
    ).json()
    assert hits["hits"], "应有命中"
    assert "孤注一掷" in hits["hits"][0]["text"]
    assert hits["hits"][0]["page"] == 2


def test_upload_rejects_non_pdf(client):
    resp = client.post(
        "/api/rulebooks/upload",
        files={"file": ("rules.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 422


def test_delete_rulebook(client):
    client.post(
        "/api/rulebooks/upload",
        files={"file": ("coc.pdf", b"%PDF", "application/pdf")},
    )
    book_id = client.get("/api/rulebooks").json()[0]["id"]

    assert client.delete(f"/api/rulebooks/{book_id}").status_code == 200
    assert client.get("/api/rulebooks").json() == []
    assert client.delete(f"/api/rulebooks/{book_id}").status_code == 404


def test_search_empty_when_no_rulebook(client):
    hits = client.get(
        "/api/rulebooks/search", params={"q": "任何", "rule_system": "coc"}
    ).json()
    assert hits == {"query": "任何", "hits": []}
