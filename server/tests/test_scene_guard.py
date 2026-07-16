"""确定性场景守卫：规划器裁定玩家本轮真实移动 → 后端把角色位置/大地图切过去（补 KP 漏切）。

修复「KP 叙述已到达新场景，但大地图仍停在旧场景」——过去场景切换只靠 KP 记得发工具。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.turn_planner import ScenePolicy, TurnPlan
from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service as cs


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scene.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    scenes = [{"id": "a", "title": "一号车厢"}, {"id": "b", "title": "七号车厢"}]
    module = Module(title="常暗之箱", rule_system="coc", npcs=[], scenes=scenes)
    pc = Character(name="山田", rule_system="coc", is_player=True, system_data={})
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active",
                    world_state={}, current_scene_id="a")
    db.add(s); db.commit()
    return s.id, module, pc


def _run(coro):
    async def collect():
        return [c async for c in coro]
    return asyncio.run(collect())


def test_scene_guard_moves_player_by_id(db_factory):
    db = db_factory(); sid, module, pc = _seed(db)
    plan = TurnPlan(scene_policy=ScenePolicy(scene_change="b"))
    chunks = _run(cs._ensure_planned_scene(db, sid, db.get(GameSession, sid), module, pc, [], plan))
    assert any("场景切换至" in c for c in chunks)
    assert db.get(GameSession, sid).current_scene_id == "b"   # 大地图锚点已跟随


def test_scene_guard_resolves_by_name(db_factory):
    """规划器给场景**名**（非 id）也能解析并切换——KP/玩家口头都用名字。"""
    db = db_factory(); sid, module, pc = _seed(db)
    plan = TurnPlan(scene_policy=ScenePolicy(scene_change="七号车厢"))
    _run(cs._ensure_planned_scene(db, sid, db.get(GameSession, sid), module, pc, [], plan))
    assert db.get(GameSession, sid).current_scene_id == "b"


def test_scene_guard_idempotent_when_already_there(db_factory):
    """KP 已自行切到目标场景（或目标就是当前场景）→ 守卫原地返回、无副作用、不重复发系统消息。"""
    db = db_factory(); sid, module, pc = _seed(db)
    plan = TurnPlan(scene_policy=ScenePolicy(scene_change="a"))   # 已在 a
    chunks = _run(cs._ensure_planned_scene(db, sid, db.get(GameSession, sid), module, pc, [], plan))
    assert chunks == []
    assert db.get(GameSession, sid).current_scene_id == "a"


def test_scene_guard_skips_unresolvable(db_factory):
    """目标解析不到真实场景 → 不写脏值、不回退到首个场景，留在原地。"""
    db = db_factory(); sid, module, pc = _seed(db)
    plan = TurnPlan(scene_policy=ScenePolicy(scene_change="不存在的地方"))
    chunks = _run(cs._ensure_planned_scene(db, sid, db.get(GameSession, sid), module, pc, [], plan))
    assert chunks == []
    assert db.get(GameSession, sid).current_scene_id == "a"


def test_scene_guard_noop_when_field_empty(db_factory):
    """规划器未裁定移动（仅讨论/打算去某地）→ 字段为空 → 守卫不动。"""
    db = db_factory(); sid, module, pc = _seed(db)
    plan = TurnPlan(scene_policy=ScenePolicy(scene_change=None))
    chunks = _run(cs._ensure_planned_scene(db, sid, db.get(GameSession, sid), module, pc, [], plan))
    assert chunks == []
    assert db.get(GameSession, sid).current_scene_id == "a"
