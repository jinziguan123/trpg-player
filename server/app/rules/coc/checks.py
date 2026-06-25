"""CoC 7th Edition 检定逻辑"""

from app.rules.base import CheckResult
from app.rules.dice import roll_percentile


def resolve_skill_check(
    character_data: dict,
    skill_name: str,
    difficulty: str = "normal",
) -> CheckResult:
    """CoC 技能检定

    难度等级:
    - normal: 普通（≤ 技能值）
    - hard: 困难（≤ 技能值/2）
    - extreme: 极难（≤ 技能值/5）
    """
    skills = character_data.get("skills", {})
    attrs = character_data.get("base_attributes", {})

    skill_value = skills.get(skill_name) or attrs.get(skill_name, 0)

    if difficulty == "hard":
        target = skill_value // 2
    elif difficulty == "extreme":
        target = skill_value // 5
    else:
        target = skill_value

    d100 = roll_percentile()

    if d100 == 1:
        outcome = "critical_success"
        desc = f"大成功！掷出了 01"
    elif d100 <= skill_value // 5:
        outcome = "hard_success" if difficulty != "extreme" else "success"
        desc = f"极难成功 ({d100} ≤ {skill_value // 5})"
    elif d100 <= skill_value // 2:
        if difficulty == "extreme":
            outcome = "failure"
            desc = f"失败 ({d100} > {skill_value // 5})"
        elif difficulty == "hard":
            outcome = "success"
            desc = f"困难成功 ({d100} ≤ {skill_value // 2})"
        else:
            outcome = "hard_success"
            desc = f"困难成功 ({d100} ≤ {skill_value // 2})"
    elif d100 <= target:
        outcome = "success"
        desc = f"成功 ({d100} ≤ {target})"
    elif d100 >= 96 and skill_value < 50:
        outcome = "fumble"
        desc = f"大失败！掷出了 {d100}"
    elif d100 == 100:
        outcome = "fumble"
        desc = f"大失败！掷出了 100"
    else:
        outcome = "failure"
        desc = f"失败 ({d100} > {target})"

    return CheckResult(
        skill_name=skill_name,
        skill_value=skill_value,
        roll=d100,
        target=target,
        outcome=outcome,
        description=desc,
    )


def san_check(character_data: dict, success_loss: str, failure_loss: str) -> dict:
    """理智检定

    Args:
        success_loss: 成功时的 SAN 损失，如 "0" 或 "1d3"
        failure_loss: 失败时的 SAN 损失，如 "1d6" 或 "1d10"
    """
    from app.rules.dice import roll

    system_data = character_data.get("system_data", {})
    san = system_data.get("sanity", {})
    current_san = san.get("current", 0)

    check = resolve_skill_check(
        {"skills": {"SAN": current_san}, "base_attributes": {}},
        "SAN",
    )

    if check.outcome in ("critical_success", "hard_success", "success"):
        loss_expr = success_loss
    else:
        loss_expr = failure_loss

    if loss_expr == "0":
        loss = 0
    else:
        loss = roll(loss_expr).total

    new_san = max(0, current_san - loss)

    return {
        "check": check,
        "san_loss": loss,
        "old_san": current_san,
        "new_san": new_san,
        "went_insane": loss >= current_san // 5,
    }
