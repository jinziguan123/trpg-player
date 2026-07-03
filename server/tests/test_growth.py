"""成长结算：CoC improvement_check 规则 + 会话内可成长技能识别 + 落库应用。"""

import random

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401
from app.rules.coc.engine import CoCRuleEngine
from app.rules.registry import get_engine
from app.services import growth_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_improvement_check_invariants():
    eng = CoCRuleEngine()
    random.seed(1)
    for _ in range(200):
        for v in (10, 55, 90, 99):
            r = eng.improvement_check(v)
            assert r["old_value"] == v
            assert r["new_value"] >= v
            assert r["new_value"] <= 99
            if r["improved"]:
                assert r["new_value"] > v and r["gain"] == r["new_value"] - v
            else:
                assert r["new_value"] == v and r["gain"] == 0


def test_improvement_low_skill_always_grows_on_high_roll(monkeypatch):
    eng = CoCRuleEngine()
    import app.rules.coc.engine as engmod
    monkeypatch.setattr(engmod, "roll_percentile", lambda: 80)   # 80 > 30 → 成长
    monkeypatch.setattr(engmod, "roll", lambda n: type("R", (), {"total": 5})())
    r = eng.improvement_check(30)
    assert r["improved"] and r["new_value"] == 35


def test_improvement_high_skill_no_grow_on_low_roll(monkeypatch):
    eng = CoCRuleEngine()
    import app.rules.coc.engine as engmod
    monkeypatch.setattr(engmod, "roll_percentile", lambda: 40)   # 40 <= 90 且 <=95 → 不成长
    r = eng.improvement_check(90)
    assert not r["improved"] and r["new_value"] == 90 and r["gain"] == 0


def test_base_engine_no_improvement():
    from app.rules.base import RuleEngine

    class _Dummy(RuleEngine):
        def get_rule_system_id(self): return "x"
        def get_character_schema(self): return {}
        def create_character(self, d): return d
        def validate_character(self, d): return True, []
        def resolve_check(self, *a, **k): ...
        def apply_damage(self, *a, **k): ...

    assert _Dummy().improvement_check(50) is None


def _seed(db) -> tuple[str, str]:
    module = Module(title="鬼屋", rule_system="coc", npcs=[], scenes=[], clues=[])
    char = Character(
        name="调查员", rule_system="coc", is_player=True,
        skills={"侦查": 55, "聆听": 46, "话术": 45, "潜行": 20},
    )
    db.add_all([module, char])
    db.flush()
    session = GameSession(module_id=module.id, player_character_id=char.id, status="active")
    db.add(session)
    db.commit()
    # 侦查成功、聆听失败、力量（非技能）成功、别人的话术成功
    session_service.add_event(db, session.id, "dice", "侦查成功", actor_name="系统",
                              metadata={"skill": "侦查", "outcome": "success", "actor": "调查员"})
    session_service.add_event(db, session.id, "dice", "聆听失败", actor_name="系统",
                              metadata={"skill": "聆听", "outcome": "failure", "actor": "调查员"})
    session_service.add_event(db, session.id, "dice", "力量成功", actor_name="系统",
                              metadata={"skill": "力量", "outcome": "success", "actor": "调查员"})
    session_service.add_event(db, session.id, "dice", "别人话术", actor_name="系统",
                              metadata={"skill": "话术", "outcome": "success", "actor": "别人"})
    return session.id, char.id


def test_eligible_skills_only_own_successful_skills(db_factory):
    db = db_factory()
    sid, cid = _seed(db)
    elig = growth_service.eligible_skills(db, sid, cid)
    names = [e["skill"] for e in elig]
    assert names == ["侦查"]  # 聆听失败/力量非技能/话术他人 都排除


def test_settle_growth_applies_gain(db_factory, monkeypatch):
    db = db_factory()
    sid, cid = _seed(db)
    # 强制侦查成长 +5
    monkeypatch.setattr(
        get_engine("coc"), "improvement_check",
        lambda v: {"roll": 99, "improved": True, "gain": 5, "old_value": v, "new_value": v + 5},
    )
    out = growth_service.settle_growth(db, sid, cid)
    assert out["results"] == [
        {"skill": "侦查", "roll": 99, "improved": True, "gain": 5, "old_value": 55, "new_value": 60}
    ]
    # 落库：技能值已更新
    fresh = db_factory().get(Character, cid)
    assert fresh.skills["侦查"] == 60
