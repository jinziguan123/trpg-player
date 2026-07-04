"""Anthropic Messages API Provider"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.ai.provider import LLMProvider, StreamDelta, ToolCall

logger = logging.getLogger(__name__)


def tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """OpenAI function schema → Anthropic 工具格式（统一入口约定为 OpenAI 风格）。"""
    out = []
    for tool in tools or []:
        fn = tool.get("function") or {}
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def messages_to_anthropic(messages: list[dict]) -> list[dict]:
    """把统一（OpenAI 风格）的对话消息翻译成 Anthropic 格式。

    - assistant 带 tool_calls → assistant content 里的 tool_use 块（附原文本）；
    - role="tool"（工具结果）→ user content 里的 tool_result 块；
    - 其余消息原样透传（system 已由调用方剥离）。
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            content: list[dict] = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for call in msg["tool_calls"]:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                content.append({
                    "type": "tool_use",
                    "id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }],
            })
        else:
            out.append(msg)
    return out


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API 实现"""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = "",
        api_key: str = "",
    ):
        self.model = model
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=120.0)
        base = base_url.rstrip("/") if base_url else "https://api.anthropic.com"
        self._api_url = f"{base}/v1/messages"
        self.last_usage: dict | None = None

    def _set_usage(self, u: dict | None) -> None:
        """把 Anthropic 的 {input_tokens, output_tokens} 归一为 OpenAI 形态，下游统一读 prompt_tokens。"""
        if not u:
            return
        pt = u.get("input_tokens")
        ct = u.get("output_tokens")
        if pt is None and ct is None:
            return
        self.last_usage = {
            "prompt_tokens": pt or 0,
            "completion_tokens": ct or 0,
            "total_tokens": (pt or 0) + (ct or 0),
        }

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _split_system(
        self, messages: list[dict],
    ) -> tuple[str | None, list[dict]]:
        """将 system 消息从 messages 中分离出来。

        Anthropic API 要求 system 消息通过独立的 system 参数传递，
        而非放在 messages 列表中。
        """
        system_parts: list[str] = []
        user_messages: list[dict] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(msg.get("content", ""))
            else:
                user_messages.append(msg)
        system_text = "\n".join(system_parts) if system_parts else None
        return system_text, user_messages

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str:
        system_text, user_messages = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "messages": user_messages,
            "temperature": temperature,
            # Anthropic 强制要求 max_tokens，无法省略；未指定时给一个宽松上限（仅为
            # 输出天花板，不按上限计费），避免对正常生成造成限制。
            "max_tokens": max_tokens if max_tokens is not None else 8192,
        }
        if system_text:
            payload["system"] = system_text

        resp = await self._client.post(
            self._api_url, headers=self._headers(), json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        self._set_usage(data.get("usage"))
        # Anthropic 返回格式: {"content": [{"type": "text", "text": "..."}]}
        return data["content"][0]["text"]

    def supports_vision(self) -> bool:
        return True  # Claude 3+ 系列均支持视觉

    async def complete_vision(
        self, prompt: str, images: list[tuple[str, str]], max_tokens: int | None = None,
    ) -> str:
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
            for b64, mime in images
        ]
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "temperature": 0.4,
            "messages": [{"role": "user", "content": content}],
        }
        resp = await self._client.post(self._api_url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def supports_tools(self) -> bool:
        return True

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        system_text, user_messages = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "messages": messages_to_anthropic(user_messages),
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else 8192,
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = tools_to_anthropic(tools)

        # tool_use 块的聚合状态：content_block_start 记 id/name，input_json_delta 累积
        # partial_json，content_block_stop 时产出完整调用。
        pending: dict | None = None
        u_in = u_out = 0
        async with self._client.stream(
            "POST", self._api_url, headers=self._headers(), json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                event_type = chunk.get("type", "")
                if event_type == "message_start":
                    u_in = ((chunk.get("message") or {}).get("usage") or {}).get("input_tokens") or u_in
                elif event_type == "message_delta":
                    u_out = (chunk.get("usage") or {}).get("output_tokens") or u_out
                if event_type == "content_block_start":
                    block = chunk.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        pending = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                elif event_type == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "input_json_delta" and pending is not None:
                        pending["json"] += delta.get("partial_json", "")
                    else:
                        text = delta.get("text", "")
                        if text:
                            yield StreamDelta(kind="text", text=text)
                elif event_type == "content_block_stop" and pending is not None:
                    try:
                        args = json.loads(pending["json"]) if pending["json"] else {}
                        if not isinstance(args, dict):
                            args = {}
                    except json.JSONDecodeError:
                        logger.warning("工具调用参数 JSON 解析失败: %s", pending["json"][:200])
                        args = {}
                    yield StreamDelta(kind="tool_call", tool_call=ToolCall(
                        id=pending["id"], name=pending["name"], arguments=args,
                    ))
                    pending = None
                elif event_type == "message_stop":
                    self._set_usage({"input_tokens": u_in, "output_tokens": u_out})
                    break

    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        system_text, user_messages = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "messages": user_messages,
            "temperature": temperature,
            "max_tokens": max_tokens if max_tokens is not None else 8192,
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text

        u_in = u_out = 0
        async with self._client.stream(
            "POST", self._api_url, headers=self._headers(), json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                event_type = chunk.get("type", "")
                if event_type == "message_start":
                    u_in = ((chunk.get("message") or {}).get("usage") or {}).get("input_tokens") or u_in
                elif event_type == "message_delta":
                    u_out = (chunk.get("usage") or {}).get("output_tokens") or u_out
                if event_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text
                elif event_type == "message_stop":
                    self._set_usage({"input_tokens": u_in, "output_tokens": u_out})
                    break
