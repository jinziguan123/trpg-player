"""确定性 SAN 守卫：planner 裁定本轮目睹恐怖 → 引擎确定性发理智检定，不靠 KP 记得。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.turn_planner import SanityPolicy, TurnPlan
from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service as cs
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'san.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="龙牙", rule_system="coc", is_player=True,
                   base_attributes={}, skills={},
                   system_data={"sanity": {"current": 60, "max": 99}})
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, pc


def _run(coro):
    async def collect():
        return [c async for c in coro]
    return asyncio.run(collect())


# ── plan schema ──

def test_plan_parses_sanity_field():
    plan = TurnPlan.model_validate({"sanity": {"trigger": True, "source": "墓室腐尸", "failure_loss": "1d6"}})
    assert plan.sanity.trigger is True and plan.sanity.source == "墓室腐尸"


def test_plan_sanity_sentence_shape_falls_back():
    # 模型把 sanity 写成一句话 → 退默认，不连累整份计划
    assert TurnPlan.model_validate({"sanity": "无恐怖"}).sanity.trigger is False


def test_build_message_carries_sanity():
    from app.ai.turn_planner import build_turn_plan_message
    msg = build_turn_plan_message(TurnPlan(sanity=SanityPolicy(trigger=True, source="怪物")))
    assert "sanity" in msg["content"] and "怪物" in msg["content"]


# ── 确定性守卫 ──

def test_guard_fires_san_when_planner_triggers(db_factory, monkeypatch):
    db = db_factory(); sid, pc = _seed(db)
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 99)  # 检定失败 → 扣满损失
    plan = TurnPlan(sanity=SanityPolicy(trigger=True, source="墓室腐尸", success_loss="0", failure_loss="1d6"))
    pre = session_service.get_next_sequence_num(db, sid) - 1
    chunks = _run(cs._ensure_planned_sanity(db, sid, db.get(GameSession, sid), pc, [], plan, pre))
    assert chunks                                   # 补发了 SAN
    db.refresh(pc)
    assert pc.system_data["sanity"]["current"] < 60  # 确定性扣了 SAN
    evs = session_service.get_session_events(db, sid)
    assert any(e.event_type == "dice" and (e.metadata_ or {}).get("skill") == "SAN" for e in evs)


def test_guard_skips_when_san_already_rolled_this_turn(db_factory):
    db = db_factory(); sid, pc = _seed(db)
    pre = session_service.get_next_sequence_num(db, sid) - 1
    # 模拟 KP 本轮已自行掷过 SAN
    session_service.add_event(db, sid, "dice", "龙牙｜理智检定", actor_name="系统", metadata={"skill": "SAN"})
    plan = TurnPlan(sanity=SanityPolicy(trigger=True, source="腐尸"))
    chunks = _run(cs._ensure_planned_sanity(db, sid, db.get(GameSession, sid), pc, [], plan, pre))
    assert chunks == []                             # 幂等跳过，不重复扣
    db.refresh(pc)
    assert pc.system_data["sanity"]["current"] == 60


def test_guard_noop_when_trigger_false(db_factory):
    db = db_factory(); sid, pc = _seed(db)
    pre = session_service.get_next_sequence_num(db, sid) - 1
    plan = TurnPlan(sanity=SanityPolicy(trigger=False))
    assert _run(cs._ensure_planned_sanity(db, sid, db.get(GameSession, sid), pc, [], plan, pre)) == []
