"""CoC 追逐的**纯确定性**规则引擎（抽象距离轨，P1 地基，不接 LLM/DB/广播）。

把官方追逐子系统抽象成一个数值 gap（正=逃方领先）：每轮双方按 MOV 调整做移动对抗推动 gap，
环境障碍=额外检定，gap 越过阈值判脱身/被追上。状态机/落库/前端在 P2+ 另接。
"""

from __future__ import annotations

from app.rules.coc.combat import compare_checks
from app.rules.coc.checks import resolve_skill_check


def resolve_chase_round(
    quarry_data: dict,
    pursuer_data: dict,
    *,
    skill: str = "运动",
    quarry_mov: int = 8,
    pursuer_mov: int = 8,
    hazard: dict | None = None,
) -> dict:
    """一轮追逐：逃方(quarry) vs 追方(pursuer) 移动对抗，MOV 快者得奖励骰。返回 gap 变化与明细。

    - 逃方胜 → gap +1（极难/大成功 +2）；追方胜 → gap -1（同理 -2）；平 → 不变。
    - hazard={who:'quarry'|'pursuer', skill, difficulty}：该方面对障碍检定，失败则对其不利（gap ∓1）。
    **不改状态**：调用方据 gap_delta 更新 world_state.chase.gap。
    """
    mov_diff = (quarry_mov or 0) - (pursuer_mov or 0)
    q = resolve_skill_check(
        quarry_data, skill, "normal",
        bonus=1 if mov_diff > 0 else 0, penalty=1 if mov_diff < 0 else 0,
    )
    p = resolve_skill_check(
        pursuer_data, skill, "normal",
        bonus=1 if mov_diff < 0 else 0, penalty=1 if mov_diff > 0 else 0,
    )
    w = compare_checks(q, p)
    if w == "a":
        gap_delta = 2 if q.tier in ("extreme", "critical") else 1
    elif w == "b":
        gap_delta = -(2 if p.tier in ("extreme", "critical") else 1)
    else:
        gap_delta = 0

    hazard_result = None
    if hazard:
        who = hazard.get("who", "quarry")
        hz_data = quarry_data if who == "quarry" else pursuer_data
        hz = resolve_skill_check(
            hz_data, hazard.get("skill", "敏捷"), hazard.get("difficulty", "normal"),
        )
        hazard_result = {"who": who, "check": hz, "passed": hz.meets_difficulty}
        if not hz.meets_difficulty:
            gap_delta += -1 if who == "quarry" else 1

    return {
        "gap_delta": gap_delta,
        "quarry_check": q,
        "pursuer_check": p,
        "hazard": hazard_result,
    }


def check_chase_end(gap: int, escape_at: int, caught_at: int) -> str | None:
    """gap 越过阈值判定：≥escape_at → 'escaped'（脱身）；≤caught_at → 'caught'（被追上）；否则 None。"""
    if gap >= escape_at:
        return "escaped"
    if gap <= caught_at:
        return "caught"
    return None
