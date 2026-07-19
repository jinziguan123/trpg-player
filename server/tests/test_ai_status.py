"""开局前置校验：AI 配置状态端点 + LLM 错误归类。"""

import httpx
from fastapi.testclient import TestClient

from app.api import ai_settings
from app.main import app
from app.services.chat_service import _classify_llm_error


def test_ai_status_reports_configured(monkeypatch):
    c = TestClient(app)

    # 无激活配置 → 未就绪
    monkeypatch.setattr(ai_settings, "load_active_profile", lambda: None)
    assert c.get("/api/settings/ai/status").json()["configured"] is False

    # 有激活配置但缺 key → 未就绪
    monkeypatch.setattr(
        ai_settings, "load_active_profile",
        lambda: ai_settings.AIProfile(name="x", model_name="m", api_key=""),
    )
    assert c.get("/api/settings/ai/status").json()["configured"] is False

    # 有 key + 模型名 → 就绪
    monkeypatch.setattr(
        ai_settings, "load_active_profile",
        lambda: ai_settings.AIProfile(name="主配置", model_name="deepseek-chat", api_key="sk-x"),
    )
    body = c.get("/api/settings/ai/status").json()
    assert body["configured"] is True and body["name"] == "主配置"


def test_classify_llm_error_maps_status_and_network():
    def _http_err(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "http://x")
        return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))

    assert "API Key" in _classify_llm_error(_http_err(401))
    assert "限流" in _classify_llm_error(_http_err(429))
    assert _classify_llm_error(_http_err(500))
    assert "连接" in _classify_llm_error(httpx.ConnectError("boom"))
    assert _classify_llm_error(ValueError("其它")) == ""  # 无法归类 → 空串回落通用文案


def test_set_fast_profile_toggle(monkeypatch, tmp_path):
    """快模型标记：单选（设 A 清 B）、重复点同一个即取消；load_fast_profile 读取一致。"""
    c = TestClient(app)
    monkeypatch.setattr(ai_settings, "SETTINGS_FILE", tmp_path / "ai_settings.json")

    a = c.post("/api/settings/ai/profiles", json={"name": "A", "model_name": "m1", "api_key": "k1"}).json()
    b = c.post("/api/settings/ai/profiles", json={"name": "B", "model_name": "m2", "api_key": "k2"}).json()

    assert c.post(f"/api/settings/ai/profiles/{a['id']}/set-fast").json()["is_fast"] is True
    assert ai_settings.load_fast_profile().name == "A"

    # 换标 B → A 被清
    c.post(f"/api/settings/ai/profiles/{b['id']}/set-fast")
    assert ai_settings.load_fast_profile().name == "B"

    # 重复点 B → 取消标记，回落主模型（load_fast_profile None）
    resp = c.post(f"/api/settings/ai/profiles/{b['id']}/set-fast").json()
    assert resp["is_fast"] is False and ai_settings.load_fast_profile() is None

    assert c.post("/api/settings/ai/profiles/nonexistent/set-fast").status_code == 404


def test_reveal_key_and_duplicate_profile(monkeypatch, tmp_path):
    """列表/增改响应里 key 恒掩码；/key 端点返回明文供「显示/复制」；
    /duplicate 完整拷贝（含真实 key）、命名「X 副本」、不激活不标快。"""
    c = TestClient(app)
    monkeypatch.setattr(ai_settings, "SETTINGS_FILE", tmp_path / "ai_settings.json")

    a = c.post("/api/settings/ai/profiles", json={
        "name": "A", "model_name": "m", "api_key": "sk-verylongsecret1234",
    }).json()
    assert "****" in a["api_key"]  # 响应恒掩码

    real = c.get(f"/api/settings/ai/profiles/{a['id']}/key").json()
    assert real["api_key"] == "sk-verylongsecret1234"

    dup = c.post(f"/api/settings/ai/profiles/{a['id']}/duplicate").json()
    assert dup["name"] == "A 副本"
    assert dup["is_active"] is False and dup["is_fast"] is False
    assert "****" in dup["api_key"]  # 响应仍掩码
    # 但落盘的是真实 key：副本可直接使用
    assert c.get(f"/api/settings/ai/profiles/{dup['id']}/key").json()["api_key"] == "sk-verylongsecret1234"

    assert c.get("/api/settings/ai/profiles/nope/key").status_code == 404
    assert c.post("/api/settings/ai/profiles/nope/duplicate").status_code == 404


def test_update_profile_persists_comfyui_fields(monkeypatch, tmp_path):
    """回归：PUT 更新必须应用 image_backend/comfyui_* 三字段（此前模型收了、应用漏了，静默丢弃）。"""
    c = TestClient(app)
    monkeypatch.setattr(ai_settings, "SETTINGS_FILE", tmp_path / "ai_settings.json")

    p = c.post("/api/settings/ai/profiles", json={"name": "A", "model_name": "m", "api_key": "k"}).json()
    r = c.put(f"/api/settings/ai/profiles/{p['id']}", json={
        "name": "A",
        "image_backend": "comfyui",
        "comfyui_base_url": "http://172.30.18.236:8188",
        "comfyui_workflow": '{"1": {}}',
    })
    assert r.status_code == 200, r.text
    saved = ai_settings._load_profiles()[0]
    assert saved.image_backend == "comfyui"
    assert saved.comfyui_base_url == "http://172.30.18.236:8188"
    assert saved.comfyui_workflow == '{"1": {}}'
