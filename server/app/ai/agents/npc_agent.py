from __future__ import annotations

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider


class NPCAgent(BaseAgent):
    def __init__(self, llm: LLMProvider, npc_id: str):
        super().__init__(llm, temperature=0.9)
        self.npc_id = npc_id

    async def respond(self, messages: list[dict]) -> str:
        # 不限制输出长度：推理类模型的 reasoning 会占预算，硬上限会让 NPC 回话被吃空。
        return await self.generate(messages)
