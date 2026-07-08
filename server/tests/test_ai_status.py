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
