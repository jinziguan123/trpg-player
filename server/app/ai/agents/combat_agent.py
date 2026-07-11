"""战斗子代理（P3）：用**精瘦上下文**做战斗叙述 + 关键 NPC 战术决策。

刻意不带主 KP 的世界记忆/线索/RAG——只给战斗态、本轮机械结算、眼前场景一句话。
narrate 把引擎结果转成 1-3 句画面感描述；decide 让有性格的关键 NPC 选战术动作。
"""

from __future__ import annotations

import json

from app.ai.agents.base import BaseAgent
from app.ai.provider import LLMProvider

_NARRATE_SYS = (
    "你是克苏鲁的呼唤（CoC）战斗描述器。根据【本轮机械结算】用 1-3 句紧凑、有画面感的中文，"
    "描述这段交战已经发生的事：谁攻击谁、命中/被闪开/反击、受伤/重伤/濒死/倒下。"
    "硬规则：① 绝不报具体数字或骰点；② 绝不替玩家做决定或臆造未发生的结果（只写结算里给出的）；"
    "③ 不泄露 NPC 隐藏秘密；④ 保持战斗的紧张节奏；"
    "⑤ 别用「不是A，是B」这类否定式对比句堆砌张力（至多偶尔一次），直陈动作与冲击更有力。"
    "只输出这段描述本身，不要前后缀。"
)

_DECIDE_SYS = (
    "你是 CoC 战斗中的一个 NPC，正轮到你行动。依据战况与你的性格/动机，选一个战术动作。"
    "只输出 JSON（不要任何多余文字）：{\"action\":\"attack|flee|dodge\",\"target_id\":\"<存活敌方id>\","
    "\"weapon\":\"<武器名，缺省徒手格斗>\"}。attack 时 target_id 必填且须是存活的敌方。"
    "HP 很低且你性格谨慎时可选 flee。"
)


def _state_brief(state: dict) -> str:
    lines = [f"第 {state.get('round', 1)} 轮。在场："]
    for p in state.get("initiative") or []:
        lines.append(
            f"- {p['name']}(id={p['id']}, {p['side']}) HP {p['hp']}/{p['max_hp']} 状态{p['status']}"
        )
    return "\n".join(lines)


class CombatAgent(BaseAgent):
    def __init__(self, llm: LLMProvider):
        super().__init__(llm, temperature=0.8)

    async def narrate(self, state: dict, beats: list[str], scene_hint: str = "") -> str:
        if not beats:
            return ""
        user = (
            (f"【场景】{scene_hint}\n" if scene_hint else "")
            + f"【战斗态】\n{_state_brief(state)}\n\n"
            + "【本轮机械结算】\n" + "\n".join(f"- {b}" for b in beats)
        )
        try:
            return (await self.generate([
                {"role": "system", "content": _NARRATE_SYS},
                {"role": "user", "content": user},
            ]) or "").strip()
        except Exception:
            return ""   # fail-open：叙述失败不阻断战斗结算

    async def decide(self, state: dict, npc: dict, scene_hint: str = "") -> dict | None:
        user = (
            (f"【场景】{scene_hint}\n" if scene_hint else "")
            + f"你是【{npc['name']}】(id={npc['id']})，性格/动机：{npc.get('personality') or '（未知）'}。\n"
            + f"你的武器：{npc.get('weapon') or '徒手格斗'}。\n【战斗态】\n{_state_brief(state)}"
        )
        try:
            raw = await self.llm.complete(
                [{"role": "system", "content": _DECIDE_SYS}, {"role": "user", "content": user}],
                temperature=0.5, response_format={"type": "json_object"},
            )
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("action"):
                return data
        except Exception:
            pass
        return None   # 解析失败 → 上层回落启发式
