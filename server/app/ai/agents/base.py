from __future__ import annotations

from collections.abc import AsyncIterator

from app.ai.provider import LLMProvider


class BaseAgent:
    def __init__(self, llm: LLMProvider, temperature: float = 0.8):
        self.llm = llm
        self.temperature = temperature

    async def generate(
        self, messages: list[dict], max_tokens: int | None = None
    ) -> str:
        return await self.llm.complete(
            messages, temperature=self.temperature, max_tokens=max_tokens
        )

    async def stream(
        self, messages: list[dict], max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        async for chunk in self.llm.stream(
            messages, temperature=self.temperature, max_tokens=max_tokens
        ):
            yield chunk
