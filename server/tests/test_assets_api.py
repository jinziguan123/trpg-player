"""素材库 API 回归：上传/列表/按类型过滤/取图/删除/内置不可删。"""

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Asset, Base  # noqa: F401 注册表

# 1x1 透明 PNG
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'a.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app), TestingSession
    app.dependency_overrides.clear()


def _upload(c, name="石棺", kind="furniture", tags="埃及,石"):
    return c.post("/api/assets", files={"file": ("sarcophagus.png", PNG_1x1, "image/png")},
                  data={"name": name, "kind": kind, "tags": tags})


def test_upload_list_image_delete(client):
    c, _ = client
    r = _upload(c)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["name"] == "石棺" and a["kind"] == "furniture"
    assert a["tags"] == ["埃及", "石"]
    assert a["image_url"] == f"/api/assets/{a['id']}/image"

    # 列表 + 取图
    assert any(x["id"] == a["id"] for x in c.get("/api/assets").json())
    img = c.get(a["image_url"])
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/")

    # 删除后取图 404
    assert c.delete(f"/api/assets/{a['id']}").status_code == 200
    assert c.get(a["image_url"]).status_code == 404


def test_kind_filter(client):
    c, _ = client
    _upload(c, name="地板", kind="floor")
    _upload(c, name="书桌", kind="furniture")
    floors = c.get("/api/assets", params={"kind": "floor"}).json()
    assert len(floors) == 1 and floors[0]["name"] == "地板"


def test_reject_non_image(client):
    c, _ = client
    r = c.post("/api/assets", files={"file": ("x.txt", b"hello", "text/plain")},
               data={"name": "x", "kind": "item"})
    assert r.status_code == 400


def test_set_default(client):
    c, _ = client
    a = _upload(c, name="地板A", kind="floor").json()
    b = _upload(c, name="地板B", kind="floor").json()
    # 设 b 为默认 → b 在前、is_default True；a 取消
    assert c.post(f"/api/assets/{b['id']}/default").json()["is_default"] is True
    floors = c.get("/api/assets", params={"kind": "floor"}).json()
    assert floors[0]["id"] == b["id"] and floors[0]["is_default"] is True
    assert next(x for x in floors if x["id"] == a["id"])["is_default"] is False
    # 改设 a → 互斥
    c.post(f"/api/assets/{a['id']}/default")
    floors2 = c.get("/api/assets", params={"kind": "floor"}).json()
    assert floors2[0]["id"] == a["id"]
    assert sum(1 for x in floors2 if x["is_default"]) == 1


def test_builtin_not_deletable(client):
    c, Session = client
    db = Session()
    db.add(Asset(id="b1", name="内置墙", kind="wall", filename="b1.png", builtin=True))
    db.commit(); db.close()
    assert c.delete("/api/assets/b1").status_code == 400


def test_edit_asset_name_kind(client):
    c, _ = client
    a = _upload(c, name="旧名", kind="furniture").json()
    r = c.patch(f"/api/assets/{a['id']}", json={"name": "新名", "kind": "item", "tags": ["x", "y"]})
    assert r.status_code == 200
    j = r.json()
    assert j["name"] == "新名" and j["kind"] == "item" and j["tags"] == ["x", "y"]
    # 未知类别拒绝
    assert c.patch(f"/api/assets/{a['id']}", json={"kind": "不存在"}).status_code == 400


def test_category_crud(client):
    c, _ = client
    # 列表含内置
    cats = c.get("/api/asset-categories").json()
    assert any(x["key"] == "floor" and x["builtin"] for x in cats)
    # 新增自定义
    assert c.post("/api/asset-categories", json={"key": "trap", "label": "陷阱"}).json()["label"] == "陷阱"
    # key 与内置冲突 → 拒绝
    assert c.post("/api/asset-categories", json={"key": "floor", "label": "x"}).status_code == 400
    # 改名
    assert c.put("/api/asset-categories/trap", json={"label": "机关"}).json()["label"] == "机关"
    # 内置不可改名/删除
    assert c.put("/api/asset-categories/floor", json={"label": "x"}).status_code == 400
    assert c.delete("/api/asset-categories/floor").status_code == 400
    # 自定义可删（无素材时）
    assert c.delete("/api/asset-categories/trap").status_code == 200
    # 用 patch 把素材改成自定义类别后，该类别不可删
    c.post("/api/asset-categories", json={"key": "trap2", "label": "陷阱2"})
    a = _upload(c, kind="furniture").json()
    c.patch(f"/api/assets/{a['id']}", json={"kind": "trap2"})
    assert c.delete("/api/asset-categories/trap2").status_code == 400
