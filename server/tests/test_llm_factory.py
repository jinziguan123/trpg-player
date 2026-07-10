"""OpenAI 兼容流式解析的健壮性：空 choices / 坏 JSON / 心跳行不应中断整段流。"""

import asyncio
import json

import pytest

from app.ai import llm_factory
from app.ai.llm_factory import OpenAICompatProvider


def test_get_llm_uses_active_profile(monkeypatch):
    """AI 唯一真源是设置页的激活 profile；据其协议/密钥建 Provider（不再读 .env）。"""
    from app.api import ai_settings

    monkeypatch.setattr(
        ai_settings, "load_active_profile",
        lambda: ai_settings.AIProfile(
            name="p", protocol="openai", model_name="deepseek-chat",
            base_url="https://x", api_key="sk-live",
        ),
    )
    llm = llm_factory.get_llm()
    assert isinstance(llm, OpenAICompatProvider) and llm.model == "deepseek-chat"


def test_get_llm_raises_without_active_profile(monkeypatch):
    """无激活配置时抛可读错误（此前会静默回退 .env——.env 已移除）。"""
    from app.api import ai_settings

    monkeypatch.setattr(ai_settings, "load_active_profile", lambda: None)
    with pytest.raises(ValueError, match="未配置可用的 AI 模型"):
        llm_factory.get_llm()


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


def test_complete_retries_transient_drop_then_succeeds(monkeypatch):
    """非流式补全遇到连接被中途掐断（RemoteProtocolError）应自动重试，下一次成功即返回。
    对应模组解析报的 incomplete chunked read。"""
    import httpx

    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "解析结果"}}], "usage": {}}

    async def flaky_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection")   # 首次瞬时中断
        return _Resp()

    async def _nosleep(_):
        pass

    monkeypatch.setattr(prov._client, "post", flaky_post)
    monkeypatch.setattr("app.ai.providers.openai_compat.asyncio.sleep", _nosleep)

    out = asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert out == "解析结果" and calls["n"] == 2   # 重试一次后成功


def test_complete_4xx_not_retried(monkeypatch):
    """4xx（鉴权/参数）不重试，立即抛。"""
    import httpx

    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}

    class _Resp:
        status_code = 401

        def raise_for_status(self):
            raise httpx.HTTPStatusError("unauth", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(401))

    async def post(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(prov._client, "post", post)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert calls["n"] == 1   # 未重试


def test_stream_skips_empty_choices(monkeypatch):
    prov = OpenAICompatProvider(model="x", api_key="k")
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "你好"}}]}),
        "data: " + json.dumps({"choices": []}),               # 空 choices —— 曾触发 IndexError 整段断流
        "data: " + json.dumps({"choices": [{"delta": {}}]}),  # 有 choices 但无 content
        ": keep-alive",                                        # 非 data 心跳行
        "data: not-json",                                      # 坏 JSON
        "data: " + json.dumps({"choices": [{"delta": {"content": "世界"}}]}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(prov._client, "stream", lambda *a, **k: _FakeStreamCtx(lines))

    async def collect():
        return [c async for c in prov.stream([{"role": "user", "content": "hi"}])]

    assert asyncio.run(collect()) == ["你好", "世界"]
