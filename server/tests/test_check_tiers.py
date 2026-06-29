"""检定达成等级（六档）回归：纯按骰值 vs 技能值算出的 tier，与要求难度无关。"""

import app.rules.coc.checks as checks
from app.rules.coc.checks import achieved_tier, resolve_skill_check, TIER_LABEL_CN


def test_achieved_tier_thresholds():
    # 技能值 60：极难≤12，困难≤30，普通≤60
    assert achieved_tier(1, 60) == "critical"     # 01 恒大成功
    assert achieved_tier(10, 60) == "extreme"     # ≤12
    assert achieved_tier(25, 60) == "hard"        # ≤30
    assert achieved_tier(55, 60) == "regular"     # ≤60
    assert achieved_tier(70, 60) == "fail"        # >60 普通失败
    assert achieved_tier(100, 60) == "fumble"     # 100 大失败
    # 技能值 <50 时 96-99 也是大失败
    assert achieved_tier(97, 40) == "fumble"
    assert achieved_tier(97, 60) == "fail"        # 技能≥50 时 96-99 只是普通失败


def test_tier_is_independent_of_required_difficulty(monkeypatch):
    """同一骰值，要求难度变化只影响 meets_difficulty，不改 achieved tier。"""
    monkeypatch.setattr(checks, "roll_percentile", lambda: 25)  # 技能60 下属困难级
    cdata = {"skills": {"侦查": 60}, "base_attributes": {}}
    r_normal = resolve_skill_check(cdata, "侦查", "normal")
    r_hard = resolve_skill_check(cdata, "侦查", "hard")
    r_extreme = resolve_skill_check(cdata, "侦查", "extreme")
    assert r_normal.tier == r_hard.tier == r_extreme.tier == "hard"  # 达成等级恒为困难
    assert r_normal.meets_difficulty is True    # 25≤60 普通过线
    assert r_hard.meets_difficulty is True      # 25≤30 困难过线
    assert r_extreme.meets_difficulty is False  # 25>12 极难不过线


def test_extreme_distinguished_from_hard(monkeypatch):
    """旧实现里极难成功被并进困难；现在六档分明。"""
    monkeypatch.setattr(checks, "roll_percentile", lambda: 10)  # 技能60 下属极难级
    cdata = {"skills": {"侦查": 60}, "base_attributes": {}}
    assert resolve_skill_check(cdata, "侦查", "normal").tier == "extreme"
    assert TIER_LABEL_CN["extreme"] == "极难成功"
