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


def test_check_continuation_fires_san_via_run_kp_turn(db_factory, monkeypatch):
    """检定后续写(sanity_guard=True)：KP 漏发 SAN，但叙事后现跑 planner 裁定目睹恐怖 → 确定性补发。

    复现问题二：恐怖由『侦查检定成功』才揭示，回合起点的 plan 看不到；本修复在叙事之后补跑
    planner（此时上下文已含刚揭示的恐怖）驱动确定性 SAN 守卫。
    """
    db = db_factory(); sid, pc = _seed(db)
    gs = db.get(GameSession, sid)
    module = db.get(Module, gs.module_id)
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 99)   # SAN 失败 → 扣损失

    async def _fake_stream(kp, messages, res, **kw):
        res[0] = "手电照亮了那具扭曲的尸体，面部中央裂开一道缝……"   # 恐怖描写，但**不发 [SAN_CHECK]**
        res[1] = res[0]
        for _ in ():
            yield ""   # 空异步生成器

    async def _fake_planner(llm, messages):
        return TurnPlan(sanity=SanityPolicy(
            trigger=True, source="扭曲的尸体", success_loss="0", failure_loss="1d6"))

    async def _noop_finish(db, sid, llm):
        return None

    monkeypatch.setattr(cs, "KPAgent", lambda llm: object())
    monkeypatch.setattr(cs, "get_llm", lambda: object())
    monkeypatch.setattr(cs, "get_fast_llm", lambda: object())
    monkeypatch.setattr(cs, "_stream_narration_filtered", _fake_stream)
    monkeypatch.setattr(cs, "build_kp_context", lambda *a, **k: [{"role": "system", "content": "x"}])
    monkeypatch.setattr(cs, "_module_excerpts_for_context", lambda *a, **k: [])
    monkeypatch.setattr(cs.turn_planner, "run_turn_planner", _fake_planner)
    monkeypatch.setattr(cs, "_finish_generation", _noop_finish)

    asyncio.run(cs._run_kp_turn(db, sid, gs, module, pc, [], "续写", sanity_guard=True))

    evs = session_service.get_session_events(db, sid)
    assert any(e.event_type == "dice" and (e.metadata_ or {}).get("skill") == "SAN" for e in evs)
    db.refresh(pc)
    assert pc.system_data["sanity"]["current"] < 60   # 确定性扣了 SAN


def test_check_continuation_no_guard_when_flag_off(db_factory, monkeypatch):
    """默认 sanity_guard=False（普通 KP 续写）：即便 planner 会触发也不补跑、不发 SAN。"""
    db = db_factory(); sid, pc = _seed(db)
    gs = db.get(GameSession, sid)
    module = db.get(Module, gs.module_id)
    ran = {"planner": False}

    async def _fake_stream(kp, messages, res, **kw):
        res[0] = res[1] = "一段平静的旁白。"
        for _ in ():
            yield ""

    async def _fake_planner(llm, messages):
        ran["planner"] = True
        return TurnPlan(sanity=SanityPolicy(trigger=True))

    monkeypatch.setattr(cs, "KPAgent", lambda llm: object())
    monkeypatch.setattr(cs, "get_llm", lambda: object())
    monkeypatch.setattr(cs, "get_fast_llm", lambda: object())
    monkeypatch.setattr(cs, "_stream_narration_filtered", _fake_stream)
    monkeypatch.setattr(cs, "build_kp_context", lambda *a, **k: [{"role": "system", "content": "x"}])
    monkeypatch.setattr(cs, "_module_excerpts_for_context", lambda *a, **k: [])
    monkeypatch.setattr(cs.turn_planner, "run_turn_planner", _fake_planner)

    async def _noop_finish(db, sid, llm):
        return None
    monkeypatch.setattr(cs, "_finish_generation", _noop_finish)

    asyncio.run(cs._run_kp_turn(db, sid, gs, module, pc, [], "续写"))   # 无 sanity_guard
    assert ran["planner"] is False                                     # 没有多跑 planner
    evs = session_service.get_session_events(db, sid)
    assert not any((e.metadata_ or {}).get("skill") == "SAN" for e in evs)
