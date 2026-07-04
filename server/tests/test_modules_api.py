"""模组手动新建/编辑/查看 API 回归。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.modules import (
    _convert_doc_to_text,
    _decode_text,
    _extract_doc_text,
    _normalize_image,
    _select_pdf_images,
)
from app.database import get_db
from app.main import app
from app.models import Base, Module  # noqa: F401 注册表


def test_parse_module_images(monkeypatch):
    """图片模组：支持视觉时据图识别出结构；不支持时报错。"""
    import asyncio
    import json as _json
    from app.services import module_service as ms

    class Vision:
        def supports_vision(self): return True
        async def complete_vision(self, prompt, images, max_tokens=None):
            assert images and "JSON" in prompt
            return "```json\n" + _json.dumps({"title": "图片模组", "scenes": [], "npcs": [], "clues": []}) + "\n```"

    class TextOnly:
        def supports_vision(self): return False

    monkeypatch.setattr(ms, "get_llm", lambda: Vision())
    out = asyncio.run(ms.parse_module_images([(b"\x89PNG...", "image/png")], "coc"))
    assert out["title"] == "图片模组"

    monkeypatch.setattr(ms, "get_llm", lambda: TextOnly())
    with pytest.raises(ValueError):
        asyncio.run(ms.parse_module_images([(b"x", "image/png")], "coc"))


def _png_bytes(w: int, h: int) -> bytes:
    """生成一张带渐变纹理的真实 PNG（够大、可被 Pillow 解码）。"""
    import io

    from PIL import Image
    im = Image.new("RGB", (w, h))
    im.putdata([((x * 3) % 256, (y * 5) % 256, (x + y) % 256) for y in range(h) for x in range(w)])
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_normalize_image_reencodes_and_rejects_garbage():
    """规整：合法图 → RGB JPEG（可解码）；无法解码的字节 → None。"""
    import io

    from PIL import Image
    good = _png_bytes(120, 120)
    norm = _normalize_image(None, good)
    assert norm is not None and norm[1] == "image/jpeg"
    Image.open(io.BytesIO(norm[0])).verify()      # 产出确为合法图
    assert _normalize_image(None, b"not-an-image-at-all") is None


def test_select_pdf_images_filters_sorts_and_normalizes():
    """PDF 内嵌图：过滤过小图标、按原始体积降序、统一规整为合法 JPEG；畸形图跳过。"""
    class _Img:
        def __init__(self, data):   # 只有 .data（无 .image），触发经 data 解码的兜底路径
            self.data = data

    class _Page:
        def __init__(self, images):
            self.images = images

    class _Reader:
        def __init__(self, pages):
            self.pages = pages

    big = _png_bytes(200, 200)      # ~1KB
    small = _png_bytes(120, 120)    # ~0.5KB
    reader = _Reader([
        _Page([_Img(b"x" * 100)]),                 # 太小 → 剔除（min_bytes）
        _Page([_Img(big), _Img(small), _Img(b"g" * 9000)]),  # 末个是畸形大字节 → 规整失败跳过
    ])
    out = _select_pdf_images(reader, min_bytes=300)
    assert len(out) == 2                            # 太小的与畸形的都被排除
    assert all(mime == "image/jpeg" for _, mime in out)  # 统一 JPEG
    assert _select_pdf_images(_Reader([_Page([])])) == []


def test_doc_converted_via_textutil(monkeypatch):
    """.doc：调用系统转换器（macOS textutil）取正文。"""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/textutil" if n == "textutil" else None)

    class _R:
        returncode = 0
        stdout = "古宅调查·模组正文".encode("utf-8")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    out = _convert_doc_to_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1doc")  # OLE 头
    assert out and "古宅调查·模组正文" in out


def test_doc_without_converter_raises(monkeypatch):
    """.doc 且本机无任何转换器：报友好 422，而非 500。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: None)
    with pytest.raises(Exception) as ei:
        _extract_doc_text(b"\xd0\xcf\x11\xe0", "古宅.doc")
    assert getattr(ei.value, "status_code", None) == 422


def test_decode_text_handles_non_utf8():
    """上传的中文 txt 常是 GBK 编码——以前直接 utf-8 解码会 500，现在能容错解码。"""
    s = "失踪的考古队·墓室秘闻"
    assert _decode_text(s.encode("utf-8")) == s          # UTF-8
    assert _decode_text(s.encode("gb18030")) == s        # GBK/GB2312（报错那种）
    assert _decode_text("﻿".encode("utf-8") + s.encode("utf-8")) == s  # 带 BOM


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
