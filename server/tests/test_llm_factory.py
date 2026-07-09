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
