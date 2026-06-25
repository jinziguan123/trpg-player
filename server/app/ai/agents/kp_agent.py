from __future__ import annotations

from collections.abc import AsyncIterator

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider


class KPAgent(BaseAgent):
    def __init__(self, llm: LLMProvider):
        super().__init__(llm, temperature=0.85)

    async def narrate(self, messages: list[dict]) -> AsyncIterator[str]:
        async for chunk in self.stream(messages):
            yield chunk
