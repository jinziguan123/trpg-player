"""LLM Provider 工厂：按激活配置创建对应厂商的 Provider。

与具体厂商解耦——文件名不绑定任何一家。新增视觉/多模态 Provider 也从这里接入。
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.ai.provider import LLMProvider


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容协议的通用 Provider（DeepSeek / OpenAI / 任意 OpenAI 兼容端点）。"""

    def __init__(self, model: str = "deepseek-chat", base_url: str = "", api_key: str = ""):
        self.model = model
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=120.0)
        base = base_url.rstrip("/") if base_url else "https://api.deepseek.com"
        self._api_url = f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> str:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        resp = await self._client.post(
            self._api_url, headers=self._headers(), json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self._client.stream(
            "POST", self._api_url, headers=self._headers(), json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                chunk = json.loads(data_str)
                delta = chunk["choices"][0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content


def get_llm() -> LLMProvider:
    """根据激活的配置创建对应的 LLM Provider。

    - protocol="anthropic" -> AnthropicProvider
    - protocol="openai"（默认）-> OpenAICompatProvider（兼容 OpenAI API）
    - 没有激活配置时，回退到 .env 环境变量
    """
    from app.api.ai_settings import load_active_profile

    profile = load_active_profile()

    if profile:
        if profile.protocol == "anthropic":
            from app.ai.anthropic import AnthropicProvider
            return AnthropicProvider(
                model=profile.model_name,
                base_url=profile.base_url,
                api_key=profile.api_key,
            )
        return OpenAICompatProvider(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=profile.api_key,
        )

    # 没有激活配置，回退到 .env 配置
    from app.config import settings
    return OpenAICompatProvider(
        model="deepseek-chat",
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
    )
