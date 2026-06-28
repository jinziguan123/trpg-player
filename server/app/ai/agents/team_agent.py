from __future__ import annotations

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider


class TeamAgent(BaseAgent):
    """AI 队友决策 agent：根据当前局面输出一次结构化行动意图（JSON）。

    只负责调用 LLM 取原始文本，JSON 解析与失败兜底（hold）由编排层处理，
    避免在 agent 内部吞掉异常。
    """

    def __init__(self, llm: LLMProvider, character_id: str):
        super().__init__(llm, temperature=0.8)
        self.character_id = character_id

    async def decide(self, messages: list[dict]) -> str:
        # 不限制输出长度：推理类模型会先吐 reasoning，512 的硬上限会把 JSON 决策吃空，
        # 导致队友长期静默。决策本身很短，不设上限也不会浪费。
        return await self.llm.complete(
            messages,
            temperature=self.temperature,
            response_format={"type": "json_object"},
        )
