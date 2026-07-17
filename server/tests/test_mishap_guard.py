"""确定性大失败反噬守卫：规划器裁定「大失败+动作有身体危险」→ 后端确定性扣 HP（补 KP 漏发）。

复现你的场景：踢正在燃烧的燃烧瓶掷出大失败 → 火焰烧到自己 → 掉血。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.turn_planner import MishapPolicy, TurnPlan
from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service as cs
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mishap.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, hp=15):
    module = Module(title="常暗之箱", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="江户川龙牙", rule_system="coc", is_player=True,
                   system_data={"hitPoints": {"current": hp, "max": hp}})
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, pc


def _run(coro):
    async def collect():
        return [c async for c in coro]
    return asyncio.run(collect())


def _hp(db, pc_id):
    return db.get(Character, pc_id).system_data["hitPoints"]["current"]


# ── MishapPolicy 字段容错 ──

def test_hp_delta_容错取负夹紧():
    """hp_delta 恒为伤害：'-3'/正数/1d3/null 都要收敛成合理负整数或 0（绝不因此丢整份计划）。"""
    assert TurnPlan.model_validate({"mishap": {"hp_delta": "-3"}}).mishap.hp_delta == -3
    assert TurnPlan.model_validate({"mishap": {"hp_delta": 3}}).mishap.hp_delta == -3    # 正数→取负
    assert TurnPlan.model_validate({"mishap": {"hp_delta": "1d3"}}).mishap.hp_delta == 0  # 骰式→取不到→0
    assert TurnPlan.model_validate({"mishap": {"hp_delta": None}}).mishap.hp_delta == 0
    assert TurnPlan.model_validate({"mishap": {"hp_delta": -99}}).mishap.hp_delta == -8   # 夹在 -8


# ── 守卫行为 ──

def test_大失败反噬确定性扣血(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=15)
    plan = TurnPlan(mishap=MishapPolicy(
        trigger=True, hp_delta=-3, target="江户川龙牙", reason="踢翻的燃烧瓶把火焰溅到腿上"))
    chunks = _run(cs._ensure_planned_mishap(db, sid, pc, [], plan, pre_gen_seq=0))
    assert any("受到 3 点伤害" in c for c in chunks)
    assert _hp(db, pc.id) == 12


def test_非触发或非负不扣血(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=15)
    # trigger=False（非身体危险的大失败，如图书馆检定）→ 不动
    _run(cs._ensure_planned_mishap(db, sid, pc, [], TurnPlan(mishap=MishapPolicy(trigger=False)), 0))
    assert _hp(db, pc.id) == 15
    # trigger 但 hp_delta 非负 → 不动
    _run(cs._ensure_planned_mishap(db, sid, pc, [],
         TurnPlan(mishap=MishapPolicy(trigger=True, hp_delta=0)), 0))
    assert _hp(db, pc.id) == 15


def test_kp已自行扣血则幂等跳过(db_factory):
    """KP 本轮已自发 HP_CHANGE 扣过血 → 守卫不重复伤害（按 hp_change<0 事件判定）。"""
    db = db_factory(); sid, pc = _seed(db, hp=15)
    # 模拟 KP 已扣血：落一条 hp_change<0 的系统事件（seq> pre_gen_seq）
    session_service.add_event(db, sid, "system", "江户川龙牙 受到 4 点伤害",
                              actor_name="系统", metadata={"hp_change": -4})
    plan = TurnPlan(mishap=MishapPolicy(trigger=True, hp_delta=-3, reason="x"))
    chunks = _run(cs._ensure_planned_mishap(db, sid, pc, [], plan, pre_gen_seq=0))
    assert chunks == []
    assert _hp(db, pc.id) == 15   # 守卫没再扣（KP 那 4 点由其自己的 HP_CHANGE 结算，此处不模拟）
