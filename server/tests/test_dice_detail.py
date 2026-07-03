"""奖励/惩罚骰真机制 + 每颗骰子明细（供前端 3D 骰子动画严格还原）。

覆盖：
- roll_percentile_detailed 的取优/取劣/抵消、result==compose(tens_kept, units)（含 100 边界）；
- resolve_skill_check 透传 bonus/penalty，无奖惩时行为与旧版一致；
- _exec_dice_check / 对抗骰 / SAN 的 dice 明细结构符合契约；
- 暗投/暗骰不落 check 明细（避免反推成败）。
掷骰随机，故用 monkeypatch 钉死或只断言结构不变量。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.rules.dice as dice
import app.rules.coc.checks as checks
from app.models import (  # noqa: F401 注册表
    Base, Character, EventLog, GameSession, Module, SessionParticipant,
)
from app.rules.coc.checks import resolve_skill_check
from app.rules.dice import compose_d100, decompose_d100, roll_percentile_detailed
from app.services import chat_service, session_service


# ---------- 纯函数：奖惩骰与合成 ----------

def test_compose_and_decompose_roundtrip():
    # 00 + 0 视作 100
    assert compose_d100(0, 0) == 100
    assert compose_d100(40, 5) == 45
    assert compose_d100(0, 5) == 5
    assert decompose_d100(100) == (0, 0)
    assert decompose_d100(45) == (40, 5)
    assert decompose_d100(5) == (0, 5)
    for d in range(1, 101):
        t, u = decompose_d100(d)
        assert compose_d100(t, u) == d


def test_no_bonus_penalty_single_tens():
    d = roll_percentile_detailed()
    assert len(d.tens) == 1
    assert d.tens_kept == d.tens[0]
    assert 1 <= d.result <= 100
    assert d.result == compose_d100(d.tens_kept, d.units)


def _patch_rolls(monkeypatch, units_digit, tens_digits):
    """按 roll_percentile_detailed 的调用顺序钉死：先 units，再各十位（randint 返回 0-9 数字）。"""
    seq = iter([units_digit, *tens_digits])
    monkeypatch.setattr(dice.random, "randint", lambda a, b: next(seq))


def test_bonus_takes_lowest_tens(monkeypatch):
    _patch_rolls(monkeypatch, 3, [7, 2, 5])       # units=3；三个十位 70/20/50
    d = roll_percentile_detailed(bonus=2)
    assert d.tens == [70, 20, 50]
    assert d.tens_kept == 20                       # 奖励骰取最有利（最小十位）
    assert d.units == 3
    assert d.result == compose_d100(20, 3) == 23


def test_penalty_takes_highest_tens(monkeypatch):
    _patch_rolls(monkeypatch, 7, [3, 8])          # units=7；两个十位 30/80
    d = roll_percentile_detailed(penalty=1)
    assert d.tens == [30, 80]
    assert d.tens_kept == 80                       # 惩罚骰取最不利（最大十位）
    assert d.result == compose_d100(80, 7) == 87


def test_bonus_penalty_cancel(monkeypatch):
    # bonus=2, penalty=2 → 净 0 → 只掷 1 个十位（不加掷、不取优取劣）
    calls = {"n": 0}

    def fake(a, b):
        calls["n"] += 1
        return 4 if calls["n"] == 1 else 5        # 第 1 次是 units=4，其后十位=50

    monkeypatch.setattr(dice.random, "randint", fake)
    d = roll_percentile_detailed(bonus=2, penalty=2)
    assert calls["n"] == 2                          # 净 0：1 次 units + 1 次十位
    assert d.tens == [50] and d.tens_kept == 50
    assert d.result == 54


def test_hundred_boundary(monkeypatch):
    monkeypatch.setattr(dice.random, "randint", lambda a, b: 0)  # 十位00、个位0
    d = roll_percentile_detailed()
    assert d.result == 100 and d.tens_kept == 0 and d.units == 0


# ---------- resolve_skill_check 透传 ----------

def test_resolve_check_no_bonus_behaves_as_before(monkeypatch):
    monkeypatch.setattr(checks, "roll_percentile", lambda: 25)
    cdata = {"skills": {"侦查": 60}, "base_attributes": {}}
    r = resolve_skill_check(cdata, "侦查", "normal")
    assert r.roll == 25 and r.tier == "hard" and r.outcome == "hard_success"
    assert r.tens == [20] and r.tens_kept == 20 and r.units == 5
    assert r.bonus == 0 and r.penalty == 0
    # result 由 tens_kept + units 合成
    assert r.roll == compose_d100(r.tens_kept, r.units)


def test_resolve_check_bonus_improves(monkeypatch):
    # 基础 roll_percentile=70（十位70），奖励骰额外掷十位=10 → 取 10 → 更有利
    monkeypatch.setattr(checks, "roll_percentile", lambda: 75)  # 十位70 个位5
    monkeypatch.setattr(checks.random, "randint", lambda a, b: 1)  # 额外十位=10
    cdata = {"skills": {"侦查": 60}, "base_attributes": {}}
    r = resolve_skill_check(cdata, "侦查", "normal", bonus=1)
    assert r.tens == [70, 10]
    assert r.tens_kept == 10                       # 取最小十位
    assert r.units == 5
    assert r.roll == 15                            # compose(10,5)
    assert r.bonus == 1 and r.penalty == 0


def test_resolve_check_penalty_worsens(monkeypatch):
    monkeypatch.setattr(checks, "roll_percentile", lambda: 15)  # 十位10 个位5
    monkeypatch.setattr(checks.random, "randint", lambda a, b: 8)  # 额外十位=80
    cdata = {"skills": {"侦查": 60}, "base_attributes": {}}
    r = resolve_skill_check(cdata, "侦查", "normal", penalty=1)
    assert r.tens == [10, 80]
    assert r.tens_kept == 80                       # 取最大十位
    assert r.roll == 85


# ---------- 端到端：_exec_dice_check / 对抗 / SAN 的 dice 明细契约 ----------

@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'd.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="陵墓", rule_system="coc", scenes=[], clues=[],
        npcs=[{"id": "g", "name": "守墓人", "skills": {"潜行": 70}}],
    )
    hero = Character(name="主角", rule_system="coc", is_player=True,
                     skills={"侦查": 60, "心理学": 50},
                     system_data={"sanity": {"current": 55}})
    ally = Character(name="阿尔法", rule_system="coc", is_player=False,
                     skills={"图书馆使用": 65})
    db.add_all([module, hero, ally])
    db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session)
    db.commit()
    return module, hero, [ally], session


def _run(db, module, hero, teammates, session, kp_text, monkeypatch):
    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = ""
        result[1] = ""
        return
        yield

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    async def go():
        chunks = []
        async for ch in chat_service._process_commands(
            db, session.id, kp_text, module, hero, session, None, teammates=teammates,
        ):
            chunks.append(ch)
        return chunks

    return asyncio.run(go())


def _dice(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: "):
            d = json.loads(c[6:])
            if d.get("type") == "dice":
                out.append(d)
    return out


def _assert_check_contract(d):
    assert d["kind"] == "check"
    assert 1 <= d["result"] <= 100
    assert isinstance(d["tens"], list) and len(d["tens"]) >= 1
    assert all(t % 10 == 0 and 0 <= t <= 90 for t in d["tens"])
    assert d["tens_kept"] in d["tens"]
    assert 0 <= d["units"] <= 9
    assert d["result"] == compose_d100(d["tens_kept"], d["units"])


def test_exec_dice_check_open_has_check_detail(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    # 队友明骰自动掷
    d = _dice(_run(db, module, hero, teammates, session,
                   "[DICE_CHECK: skill=图书馆使用, char=阿尔法]", monkeypatch))[0]
    _assert_check_contract(d["metadata"]["dice"])


def test_exec_dice_check_bonus_penalty_parsed(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[DICE_CHECK: skill=图书馆使用, char=阿尔法, bonus=1, penalty=0]",
                   monkeypatch))[0]
    detail = d["metadata"]["dice"]
    assert detail["bonus"] == 1 and detail["penalty"] == 0
    assert len(detail["tens"]) == 2                # 净奖惩 1 → 多掷一个十位


def test_blind_check_has_no_detail(db_factory, monkeypatch):
    """暗投不落 check 明细（否则可反推成败）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[DICE_CHECK: skill=潜行, char=守墓人, visibility=blind]", monkeypatch))[0]
    assert d["metadata"]["blind"] is True
    assert "dice" not in d["metadata"]


def test_opposed_each_side_has_check_detail(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[OPPOSED_CHECK: a=主角, b=守墓人, skill=侦查]", monkeypatch))[0]
    meta = d["metadata"]
    assert meta["opposed"] is True
    _assert_check_contract(meta["a"]["dice"])
    _assert_check_contract(meta["b"]["dice"])


def test_san_loss_pool_detail(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[SAN_CHECK: success_loss=0, failure_loss=1d6, chars=主角, source=腐尸]",
                   monkeypatch))[0]
    meta = d["metadata"]
    pool = meta["dice"]
    assert pool["kind"] == "pool"
    assert pool["total"] == meta["san_loss"]
    # 损失为骰池时逐骰明细齐备；固定 0 损失时 dice 为空、total 即定值
    if pool["notation"] != "0":
        assert len(pool["dice"]) >= 1
        assert all(die["sides"] == 6 for die in pool["dice"])
        assert sum(die["value"] for die in pool["dice"]) + pool["modifier"] == pool["total"]
    # SAN 判定本身的 d100 明细也在
    _assert_check_contract(meta["check_dice"])
