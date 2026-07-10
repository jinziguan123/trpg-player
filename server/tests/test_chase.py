"""CoC 追逐引擎（抽象距离轨，纯确定性）单测。"""

from app.rules.coc import chase


def _seq(values):
    it = iter(values)
    return lambda: next(it)


_Q = {"skills": {"运动": 60}, "base_attributes": {}}
_P = {"skills": {"运动": 60}, "base_attributes": {}}


def test_quarry_wins_opens_gap(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([45, 80]))  # 逃成功、追失败
    r = chase.resolve_chase_round(_Q, _P)
    assert r["gap_delta"] == 1


def test_quarry_extreme_opens_two(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([10, 80]))  # 逃极难成功
    r = chase.resolve_chase_round(_Q, _P)
    assert r["gap_delta"] == 2


def test_pursuer_wins_closes_gap(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([80, 45]))  # 逃失败、追成功
    r = chase.resolve_chase_round(_Q, _P)
    assert r["gap_delta"] == -1


def test_tie_keeps_gap(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([45, 45]))  # 同级同技能 → 平
    r = chase.resolve_chase_round(_Q, _P)
    assert r["gap_delta"] == 0


def test_mov_difference_grants_bonus_die(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([45, 45]))
    # 逃方更快 → 逃方得 1 奖励骰、追方得 1 惩罚骰（直接看 check 的 bonus/penalty 已正确传入）
    r = chase.resolve_chase_round(_Q, _P, quarry_mov=9, pursuer_mov=6)
    assert r["quarry_check"].bonus == 1 and r["quarry_check"].penalty == 0
    assert r["pursuer_check"].penalty == 1 and r["pursuer_check"].bonus == 0


def test_hazard_failure_hurts_the_facing_side(monkeypatch):
    # 平局(gap_delta 0) + 逃方撞障碍失败 → gap 再 -1（更易被追上）
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([45, 45, 80]))
    r = chase.resolve_chase_round(_Q, _P, hazard={"who": "quarry", "skill": "敏捷"})
    assert r["hazard"]["passed"] is False and r["gap_delta"] == -1


def test_check_chase_end_thresholds():
    assert chase.check_chase_end(6, escape_at=6, caught_at=-3) == "escaped"
    assert chase.check_chase_end(-3, escape_at=6, caught_at=-3) == "caught"
    assert chase.check_chase_end(2, escape_at=6, caught_at=-3) is None
