"""Anthropic Messages API Provider"""

import json
from collections.abc import AsyncIterator

import httpx

from app.ai.provider import LLMProvider


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
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> str:
        system_text, user_messages = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "messages": user_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_text:
            payload["system"] = system_text

        resp = await self._client.post(
            self._api_url, headers=self._headers(), json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        # Anthropic 返回格式: {"content": [{"type": "text", "text": "..."}]}
        return data["content"][0]["text"]

    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        system_text, user_messages = self._split_system(messages)
        payload: dict = {
            "model": self.model,
            "messages": user_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text

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
                if event_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text
                elif event_type == "message_stop":
                    break
