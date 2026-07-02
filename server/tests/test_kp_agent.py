"""KPAgent 裁定轮行为回归测试。

裁定轮（注入的裁定计划带「必须发起检定」硬约束）要：
1. 临时压低采样温度，让模型只写「尝试过程」而不因高温发挥把发现/线索位置提前写出来；
2. 若模型仍漏发检定指令，确定性补上计划指定的 [DICE_CHECK] 收尾，保证本轮进入待掷骰状态。
常规叙事轮不受影响：默认高温、不追加任何指令。
"""

from __future__ import annotations

import pytest

from app.ai.agents.kp_agent import KPAgent, _CHECK_TURN_TEMPERATURE
from app.ai.turn_planner import CheckPlan, TurnPlan, build_turn_plan_message


class _ScriptedLLM:
    """按预设分片回放，并记录每次 stream 收到的温度。"""

    def __init__(self, chunks: list[str]):
        self._chunks = chunks
        self.temperatures: list[float] = []

    async def complete(self, messages, temperature=0.85, max_tokens=None):  # pragma: no cover
        return "".join(self._chunks)

    async def stream(self, messages, temperature=0.85, max_tokens=None):
        self.temperatures.append(temperature)
        for chunk in self._chunks:
            yield chunk


async def _collect(agent: KPAgent, messages: list[dict]) -> str:
    return "".join([tok async for tok in agent.narrate(messages)])


def _check_plan_message() -> dict:
    return build_turn_plan_message(
        TurnPlan(requires_check=True, check=CheckPlan(skill="侦查", difficulty="normal"))
    )


@pytest.mark.asyncio
async def test_裁定轮降温且模型自带指令时不重复追加():
    llm = _ScriptedLLM(["你俯身叩击书桌侧板。\n", "[DICE_CHECK: skill=侦查, difficulty=normal]"])
    agent = KPAgent(llm)
    out = await _collect(agent, [{"role": "system", "content": "KP"}, _check_plan_message()])
    assert llm.temperatures == [_CHECK_TURN_TEMPERATURE]
    # 模型自己发了指令：只出现一次，不追加
    assert out.count("[DICE_CHECK:") == 1
    # 事后温度复原
    assert agent.temperature == 0.85


@pytest.mark.asyncio
async def test_裁定轮漏发指令时确定性补上计划指令收尾():
    # 模型只写了尝试过程、忘了发指令（评估里复现的高频失败模式）
    llm = _ScriptedLLM(["你俯身叩击书桌侧板。", "你调整呼吸，把注意力沉到指尖。"])
    agent = KPAgent(llm)
    out = await _collect(agent, [{"role": "system", "content": "KP"}, _check_plan_message()])
    # 兜底补上计划指定的指令，且作为最后一行
    assert out.rstrip().endswith("[DICE_CHECK: skill=侦查, difficulty=normal]")
    assert out.count("[DICE_CHECK:") == 1


@pytest.mark.asyncio
async def test_常规轮不降温也不追加指令():
    llm = _ScriptedLLM(["门厅里灰尘在光柱中浮动。"])
    agent = KPAgent(llm)
    plan = build_turn_plan_message(TurnPlan(requires_check=False))
    out = await _collect(agent, [{"role": "system", "content": "KP"}, plan])
    assert llm.temperatures == [0.85]
    assert "[DICE_CHECK:" not in out
