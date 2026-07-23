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


def test_parse_module_text_resumes_truncated_json(monkeypatch):
    """长模组输出撞 max_tokens 被截断 → 自动断点续写拼接后解析成功；
    续写调用不得带 response_format=json_object（那会迫使模型重开新 JSON 而非接着写）。"""
    import asyncio
    import json as _json
    from app.services import module_service as ms

    full = _json.dumps(
        {"title": "常暗之箱", "scenes": [{"id": "scene_1", "title": "6号车厢"}],
         "npcs": [], "clues": []},
        ensure_ascii=False,
    )
    cut = len(full) // 2
    calls: list[dict] = []

    class LLM:
        async def complete(self, messages, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return full[:cut]          # 首次：截断的半截 JSON
            assert messages[-2]["content"] == full[:cut]   # 半截输出作为 assistant 上文回灌
            return full[cut:]              # 续写：从断点接着写

    monkeypatch.setattr(ms, "get_llm", lambda: LLM())
    parsed = asyncio.run(ms.parse_module_text("模组正文", "coc"))
    assert parsed["title"] == "常暗之箱" and parsed["scenes"][0]["id"] == "scene_1"
    assert calls[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in calls[1]   # 续写不带 json_object


def test_parse_module_text_falls_back_to_restarted_json(monkeypatch):
    """个别模型不接续而是整个重出一份完整 JSON → 拼接解析失败后，退而解析续写单独成篇。"""
    import asyncio
    import json as _json
    from app.services import module_service as ms

    full = _json.dumps({"title": "重出模组", "scenes": [], "npcs": [], "clues": []}, ensure_ascii=False)
    calls = {"n": 0}

    class LLM:
        async def complete(self, messages, **kw):
            calls["n"] += 1
            return full[: len(full) // 2] if calls["n"] == 1 else full  # 续写=整份重出

    monkeypatch.setattr(ms, "get_llm", lambda: LLM())
    parsed = asyncio.run(ms.parse_module_text("模组正文", "coc"))
    assert parsed["title"] == "重出模组"


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


def test_enrich_map_returns_summary_without_real_llm(client, monkeypatch):
    from app.services import module_map_service

    module_id = client.post("/api/modules", json=_payload()).json()["id"]

    async def fake_enrich(db, module):
        assert module.id == module_id
        return {"updated": True, "connections_added": 2}

    monkeypatch.setattr(module_map_service, "enrich_module_map", fake_enrich)
    response = client.post(f"/api/modules/{module_id}/map/enrich")
    assert response.status_code == 200
    assert response.json() == {"updated": True, "connections_added": 2}


def test_enrich_map_maps_missing_and_failure_status(client, monkeypatch):
    from app.services import module_map_service

    assert client.post("/api/modules/nope/map/enrich").status_code == 404
    module_id = client.post("/api/modules", json=_payload()).json()["id"]

    async def fake_failure(db, module):
        raise ValueError("模型返回坏 JSON")

    monkeypatch.setattr(module_map_service, "enrich_module_map", fake_failure)
    response = client.post(f"/api/modules/{module_id}/map/enrich")
    assert response.status_code == 400
    assert response.json()["detail"] == "模型返回坏 JSON"


def test_merge_supplement_is_conservative():
    """查漏合并铁律：只补遗漏——已有 events/字段/线索绝不被补丁覆盖或改写，输入不被修改。"""
    from app.services.module_service import _merge_supplement

    parsed = {
        "truth": "",
        "scenes": [{"id": "s1", "title": "车厢",
                    "events": [{"trigger": "进入即见血迹", "kind": "san_check", "san_loss": "0/1"}]}],
        "npcs": [{"id": "n1", "name": "循声者", "hp": 20, "armor": 2, "weapon": "撕咬"}],
        "clues": [{"id": "c1"}],
        "handouts": [],
    }
    patch = {
        "truth": "真相是列车早已驶入异界。",
        "scenes": [
            {"id": "s1", "events": [
                {"trigger": "进入即见血迹", "kind": "san_check", "san_loss": "9/9d9"},  # 重复：不覆盖
                {"trigger": "打开行李箱", "kind": "san_check", "san_loss": "1/1d4"},    # 遗漏：补上
            ]},
            {"id": "s2", "title": "新场景"},
        ],
        "npcs": [
            {"id": "n1", "hp": 99, "goals": ["吃掉全部乘客"]},   # hp 已有不覆盖；goals 缺失补上
            {"id": "n2", "name": "账房"},
        ],
        "clues": [{"id": "c1", "name": "试图改写"}, {"id": "c2", "name": "新线索"}],
        "handouts": [{"id": "h1", "title": "新手书"}],
    }
    out = _merge_supplement(parsed, patch)

    assert out["truth"] == "真相是列车早已驶入异界。"        # 原为空 → 直接取补丁
    s1 = next(s for s in out["scenes"] if s["id"] == "s1")
    assert len(s1["events"]) == 2
    assert s1["events"][0]["san_loss"] == "0/1"              # 已有机制点数值未被改
    assert any(s.get("id") == "s2" for s in out["scenes"])   # 遗漏场景补入
    n1 = next(n for n in out["npcs"] if n["id"] == "n1")
    assert n1["hp"] == 20 and n1["goals"] == ["吃掉全部乘客"]
    assert any(n.get("id") == "n2" for n in out["npcs"])
    assert next(c for c in out["clues"] if c["id"] == "c1").get("name") is None  # 已有线索不被改写
    assert any(c.get("id") == "c2" for c in out["clues"])
    assert any(h.get("id") == "h1" for h in out["handouts"])
    # 纯函数：输入未被修改
    assert parsed["truth"] == "" and len(parsed["scenes"][0]["events"]) == 1
    assert "goals" not in parsed["npcs"][0]


def test_supplement_parse_merges_and_fails_open(monkeypatch):
    """查漏自检：正常时合并补丁；LLM 炸掉 / 纯图片模组（无原文）时原样返回首轮结果。"""
    import asyncio
    import json as _json
    from app.services import module_service as ms

    parsed = {"truth": "", "scenes": [], "npcs": [], "clues": [], "handouts": []}

    class LLM:
        async def complete(self, messages, **kw):
            assert "质检员" in messages[0]["content"]
            return _json.dumps({"truth": "补上的真相", "scenes": [], "npcs": [],
                                "clues": [], "handouts": []}, ensure_ascii=False)

    monkeypatch.setattr(ms, "get_llm", lambda: LLM())
    out = asyncio.run(ms.supplement_parse("模组原文……", parsed, "coc"))
    assert out["truth"] == "补上的真相"

    class Boom:
        async def complete(self, *a, **kw):
            raise RuntimeError("provider down")

    monkeypatch.setattr(ms, "get_llm", lambda: Boom())
    assert asyncio.run(ms.supplement_parse("模组原文……", parsed, "coc")) is parsed  # fail-open

    called = {"n": 0}

    class Never:
        async def complete(self, *a, **kw):
            called["n"] += 1

    monkeypatch.setattr(ms, "get_llm", lambda: Never())
    assert asyncio.run(ms.supplement_parse("   ", parsed, "coc")) is parsed  # 无原文：零调用跳过
    assert called["n"] == 0


def test_upload_job_runs_stages_to_done(tmp_path, monkeypatch):
    """后台解析任务：逐段推进进度，完成时 status=done、percent=100、带结果摘要。"""
    import asyncio
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.api.modules as mod
    from app.services import module_service as ms

    engine = create_engine(f"sqlite:///{tmp_path / 'job.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(mod, "SessionLocal", sessionmaker(bind=engine))

    async def fake_parse(raw_text, rule_system, on_progress=None):
        return {"title": "异步模组", "scenes": [{"id": "s1", "title": "入口"}], "npcs": [], "clues": []}

    async def fake_supplement(raw_text, parsed, rule_system):
        return parsed

    monkeypatch.setattr(ms, "parse_module_text", fake_parse)
    monkeypatch.setattr(ms, "supplement_parse", fake_supplement)

    job_id = mod._job_new()
    asyncio.run(mod._run_upload_job(job_id, "原文", [], "coc"))

    job = mod._upload_jobs[job_id]
    assert job["status"] == "done" and job["percent"] == 100
    assert job["result"]["title"] == "异步模组" and job["result"]["scenes_count"] == 1


def test_upload_job_fails_with_readable_detail(monkeypatch):
    """解析炸掉：任务落成 failed + 可读 detail（沿用旧同步端点的文案），绝不无声消失。"""
    import asyncio
    import json as _json

    import app.api.modules as mod
    from app.services import module_service as ms

    async def boom(raw_text, rule_system, on_progress=None):
        raise _json.JSONDecodeError("truncated", "{", 1)

    monkeypatch.setattr(ms, "parse_module_text", boom)
    job_id = mod._job_new()
    asyncio.run(mod._run_upload_job(job_id, "原文", [], "coc"))
    job = mod._upload_jobs[job_id]
    assert job["status"] == "failed" and "截断" in job["detail"]


def test_upload_endpoint_returns_job_id_and_status(client, monkeypatch):
    """上传端点立即返回 job_id；状态端点可轮询；未知任务 404。"""
    import app.api.modules as mod

    async def noop_job(job_id, raw_text, images, rule_system):
        return None

    monkeypatch.setattr(mod, "_run_upload_job", noop_job)
    r = client.post(
        "/api/modules/upload?rule_system=coc",
        files={"files": ("测试.txt", "模组正文".encode("utf-8"), "text/plain")},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    s = client.get(f"/api/modules/upload/status/{job_id}").json()
    assert s["status"] == "running" and "percent" in s
    assert client.get("/api/modules/upload/status/nonexistent").status_code == 404
