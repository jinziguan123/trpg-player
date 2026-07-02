from __future__ import annotations

import re
from collections.abc import AsyncIterator

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider
from app.ai.turn_planner import REQUIRES_CHECK_MARKER

# 裁定轮（requires_check=true）的采样温度：明显低于常规叙事的 0.85。检定轮要求模型只写
# 「尝试过程 + 以 [DICE_CHECK] 收尾、不提前泄结果/线索位置」，这是强约束遵循而非创作发挥；
# 高温会让模型忍不住把「回音发空、摸到暗缝」这类发现写出来（既漏收尾又提前泄露），实测降温后
# 泄露显著减少。仅本轮临时压低，不影响其它叙事轮的文学性。
_CHECK_TURN_TEMPERATURE = 0.2

# 从裁定计划注入消息里抠出本轮该发的检定指令原文（build_turn_plan_message 会把它逐字写进硬约束段）。
_CHECK_DIRECTIVE_RE = re.compile(r"\[(?:DICE_CHECK|OPPOSED_CHECK):[^\]]*\]")


class KPAgent(BaseAgent):
    def __init__(self, llm: LLMProvider):
        super().__init__(llm, temperature=0.85)

    async def narrate(self, messages: list[dict]) -> AsyncIterator[str]:
        directive = _required_check_directive(messages)
        if directive is None:
            # 常规叙事轮：原样流式，温度不变。
            async for chunk in self.stream(messages):
                yield chunk
            return

        # 裁定轮：临时降温提升「只写尝试 + 以指令收尾」的服从度；同时缓冲全文，若模型仍漏发
        # 检定指令，就确定性补上计划指定的指令收尾——保证本轮一定进入待掷骰状态，不空转。
        prev_temperature = self.temperature
        self.temperature = _CHECK_TURN_TEMPERATURE
        buffer: list[str] = []
        try:
            async for chunk in self.stream(messages):
                buffer.append(chunk)
                yield chunk
        finally:
            self.temperature = prev_temperature

        text = "".join(buffer)
        if not _CHECK_DIRECTIVE_RE.search(text):
            suffix = "" if text.endswith("\n") else "\n"
            yield f"{suffix}{directive}"


def _required_check_directive(messages: list[dict]) -> str | None:
    """本轮若为「必须发起检定」的裁定轮，返回计划指定的检定指令原文；否则 None。"""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or REQUIRES_CHECK_MARKER not in content:
            continue
        match = _CHECK_DIRECTIVE_RE.search(content)
        if match:
            return match.group(0)
    return None
