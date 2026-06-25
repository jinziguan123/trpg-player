from __future__ import annotations

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider


class NPCAgent(BaseAgent):
    def __init__(self, llm: LLMProvider, npc_id: str):
        super().__init__(llm, temperature=0.9)
        self.npc_id = npc_id

    async def respond(self, messages: list[dict]) -> str:
        return await self.generate(messages, max_tokens=1024)
