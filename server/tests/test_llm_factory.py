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


class _StreamCtx:
    """模拟 httpx 的流式上下文管理器：__aenter__ 返回一个能 aiter_lines 的 resp。"""
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _LinesResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _DropResp:
    """流到一半连接被掐断（RemoteProtocolError）。"""
    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        import httpx
        raise httpx.RemoteProtocolError("peer closed connection")
        yield  # unreachable


class _401Resp:
    def raise_for_status(self):
        import httpx
        raise httpx.HTTPStatusError("unauth", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(401))


async def _nosleep(_):
    pass


def test_complete_retries_transient_drop_then_succeeds(monkeypatch):
    """补全（内部流式）遇到连接被中途掐断（RemoteProtocolError）应自动重试，下一次成功即返回。
    对应模组解析报的 incomplete chunked read。"""
    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}
    good = ["data: " + json.dumps({"choices": [{"delta": {"content": "解析结果"}}]}), "data: [DONE]"]

    def flaky_stream(*a, **k):
        calls["n"] += 1
        return _StreamCtx(_DropResp() if calls["n"] == 1 else _LinesResp(good))

    monkeypatch.setattr(prov._client, "stream", flaky_stream)
    monkeypatch.setattr("app.ai.providers.openai_compat.asyncio.sleep", _nosleep)

    out = asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert out == "解析结果" and calls["n"] == 2   # 重试一次后成功


def test_complete_4xx_not_retried(monkeypatch):
    """4xx（鉴权/参数）不重试，立即抛。"""
    import httpx

    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}

    def stream(*a, **k):
        calls["n"] += 1
        return _StreamCtx(_401Resp())

    monkeypatch.setattr(prov._client, "stream", stream)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert calls["n"] == 1   # 未重试


class _PartialThenDropResp:
    """先正常吐一个内容块，随后连接被掐断——已产出可见 token，不该重试。"""
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        import httpx
        for ln in self._lines:
            yield ln
        raise httpx.RemoteProtocolError("peer closed mid-stream")


def test_stream_chat_retries_drop_before_first_token(monkeypatch):
    """流式工具路径：首个可见 token 之前连接被掐断（Server disconnected）应自动重试并成功。
    复现线上「重新生成失败：RemoteProtocolError: Server disconnected without sending a response」。"""
    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}
    good = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "续写"}}]}),
        "data: [DONE]",
    ]

    def flaky(*a, **k):
        calls["n"] += 1
        return _StreamCtx(_DropResp() if calls["n"] == 1 else _LinesResp(good))

    monkeypatch.setattr(prov._client, "stream", flaky)
    monkeypatch.setattr("app.ai.providers.openai_compat.asyncio.sleep", _nosleep)

    async def collect():
        return [d.text async for d in prov.stream_chat([{"role": "user", "content": "hi"}])]

    assert asyncio.run(collect()) == ["续写"] and calls["n"] == 2  # 重试一次后成功


def test_stream_chat_no_retry_after_first_token(monkeypatch):
    """已吐出可见 token 后再断连：绝不重试（重试会重复已下发内容），原样抛。"""
    import httpx

    prov = OpenAICompatProvider(model="x", api_key="k")
    calls = {"n": 0}
    partial = ["data: " + json.dumps({"choices": [{"delta": {"content": "开头"}}]})]

    def stream(*a, **k):
        calls["n"] += 1
        return _StreamCtx(_PartialThenDropResp(partial))

    monkeypatch.setattr(prov._client, "stream", stream)
    monkeypatch.setattr("app.ai.providers.openai_compat.asyncio.sleep", _nosleep)

    async def collect():
        return [d.text async for d in prov.stream_chat([{"role": "user", "content": "hi"}])]

    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(collect())
    assert calls["n"] == 1   # 未重试


def test_complete_tracks_usage_into_accumulator(monkeypatch):
    """服务端 usage 会被累进当前任务的 usage_tracker 累加器（供本局累计 token 消耗）。"""
    from app.ai import usage_tracker

    prov = OpenAICompatProvider(model="x", api_key="k")
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "data: " + json.dumps({"choices": [], "usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(prov._client, "stream", lambda *a, **k: _StreamCtx(_LinesResp(lines)))

    async def run():
        usage_tracker._acc.set(usage_tracker._zero())
        await prov.complete([{"role": "user", "content": "hi"}])
        return usage_tracker.snapshot()

    snap = asyncio.run(run())
    assert snap["total_tokens"] == 12 and snap["calls"] == 1


def test_anthropic_set_usage_accumulates():
    """Anthropic 的 _set_usage 归一后也累进 usage_tracker（此前只写 last_usage、系统性漏记）。"""
    from app.ai import usage_tracker
    from app.ai.providers.anthropic import AnthropicProvider

    prov = AnthropicProvider(model="claude-x", api_key="k")
    token = usage_tracker._acc.set(usage_tracker._zero())   # 用 token 复位，避免污染后续测试的 contextvar
    try:
        prov._set_usage({"input_tokens": 10, "output_tokens": 2})
        snap = usage_tracker.snapshot()
    finally:
        usage_tracker._acc.reset(token)
    assert snap["total_tokens"] == 12 and snap["prompt_tokens"] == 10 and snap["calls"] == 1
    assert prov.last_usage["total_tokens"] == 12   # last_usage 仍照常写


def test_reasoning_effort_in_payload_omits_temperature(monkeypatch):
    """设了 reasoning_effort：payload 带上它、且去掉 temperature（推理模型多拒绝/忽略 temperature）。"""
    prov = OpenAICompatProvider(model="gpt-5.5", api_key="k", reasoning_effort="xhigh")
    captured = {}
    good = ["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "data: [DONE]"]

    def cap(method, url, headers=None, json=None):
        captured["payload"] = json
        return _StreamCtx(_LinesResp(good))

    monkeypatch.setattr(prov._client, "stream", cap)
    asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert captured["payload"]["reasoning_effort"] == "xhigh"
    assert "temperature" not in captured["payload"]


def test_no_reasoning_effort_keeps_temperature(monkeypatch):
    """未设 reasoning_effort：payload 不带该参数、照常发 temperature（非推理模型不受影响）。"""
    prov = OpenAICompatProvider(model="deepseek-chat", api_key="k")
    captured = {}
    good = ["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "data: [DONE]"]

    def cap(method, url, headers=None, json=None):
        captured["payload"] = json
        return _StreamCtx(_LinesResp(good))

    monkeypatch.setattr(prov._client, "stream", cap)
    asyncio.run(prov.complete([{"role": "user", "content": "hi"}]))
    assert "reasoning_effort" not in captured["payload"]
    assert captured["payload"]["temperature"] == 0.7


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
