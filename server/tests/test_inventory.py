"""活库存：确定性增删/使用/转让/播种，及 planner 信号的确定性入库（幂等）。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.turn_planner import ItemDelta, TurnPlan
from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service as cs
from app.services import inventory_service as inv
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'inv.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, equipment=None, ally=False):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="龙牙", rule_system="coc", is_player=True,
                   system_data={"equipment": equipment or [], "hitPoints": {"current": 10, "max": 10}})
    chars = [module, pc]
    mate = None
    if ally:
        mate = Character(name="健太", rule_system="coc", is_player=False, system_data={})
        chars.append(mate)
    db.add_all(chars); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, pc, mate


def _run(coro):
    async def collect():
        return [c async for c in coro]
    return asyncio.run(collect())


# ── service ──

def test_add_stacks_same_name(db_factory):
    db = db_factory(); _, pc, _ = _seed(db)
    inv.add_item(db, pc, "火柴", qty=3, kind="consumable")
    inv.add_item(db, pc, "火柴", qty=2, kind="consumable")
    items = inv.get_inventory(pc)
    assert len(items) == 1 and items[0]["qty"] == 5


def test_use_consumable_decrements_gear_does_not(db_factory):
    db = db_factory(); _, pc, _ = _seed(db)
    match = inv.add_item(db, pc, "火柴", qty=2, kind="consumable")
    torch = inv.add_item(db, pc, "手电筒", kind="gear")
    inv.use_item(db, pc, match["id"])
    inv.use_item(db, pc, torch["id"])
    by = {it["name"]: it for it in inv.get_inventory(pc)}
    assert by["火柴"]["qty"] == 1        # 消耗品 -1
    assert by["手电筒"]["qty"] == 1       # 装备不减
    # 用完最后一根 → 整条移除
    inv.use_item(db, pc, match["id"])
    assert "火柴" not in {it["name"] for it in inv.get_inventory(pc)}


def test_remove_partial_and_by_name(db_factory):
    db = db_factory(); _, pc, _ = _seed(db)
    inv.add_item(db, pc, "子弹", qty=6, kind="consumable")
    inv.remove_by_name(db, pc, "子弹", qty=2)
    assert inv.get_inventory(pc)[0]["qty"] == 4
    assert inv.remove_by_name(db, pc, "不存在") is None


def test_give_moves_to_teammate(db_factory):
    db = db_factory(); _, pc, mate = _seed(db, ally=True)
    key = inv.add_item(db, pc, "黄铜钥匙", kind="key")
    inv.give_item(db, pc, mate, key["id"])
    assert not inv.get_inventory(pc)
    assert inv.get_inventory(mate)[0]["name"] == "黄铜钥匙"


def test_seed_from_equipment_once(db_factory):
    db = db_factory(); _, pc, _ = _seed(db, equipment=["手电筒", "火柴", "火柴"])
    inv.seed_from_equipment(db, pc)
    by = {it["name"]: it for it in inv.get_inventory(pc)}
    assert by["手电筒"]["qty"] == 1 and by["火柴"]["qty"] == 2
    # 幂等：库存非空再播种不重复
    inv.seed_from_equipment(db, pc)
    assert len(inv.get_inventory(pc)) == 2


# ── planner 确定性入库守卫 ──

def test_planner_items_guard_adds_and_is_idempotent(db_factory):
    db = db_factory(); sid, pc, _ = _seed(db)
    session_service.add_event(db, sid, "action", "我搜查抽屉", actor_id=pc.id, actor_name=pc.name)
    plan = TurnPlan(items_gained=[ItemDelta(name="黄铜钥匙", qty=1, kind="key")])
    gs = db.get(GameSession, sid)
    chunks = _run(cs._ensure_planned_items(db, sid, gs, pc, [], plan))
    assert any('"inventory_update"' in c for c in chunks)
    assert inv.get_inventory(pc)[0]["name"] == "黄铜钥匙"
    # 重新生成同一 plan（同一行动锚）→ 幂等，不重复入库
    _run(cs._ensure_planned_items(db, sid, db.get(GameSession, sid), pc, [], plan))
    assert len(inv.get_inventory(pc)) == 1


def test_planner_items_guard_removes_lost(db_factory):
    db = db_factory(); sid, pc, _ = _seed(db)
    inv.add_item(db, pc, "火把", kind="gear")
    session_service.add_event(db, sid, "action", "我举着火把前进", actor_id=pc.id, actor_name=pc.name)
    plan = TurnPlan(items_lost=[ItemDelta(name="火把")])
    _run(cs._ensure_planned_items(db, sid, db.get(GameSession, sid), pc, [], plan))
    assert not inv.get_inventory(pc)


# ── 玩家侧端点 ──

def test_inventory_endpoints_use_drop_give(tmp_path):
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.models import SessionParticipant

    engine = create_engine(
        f"sqlite:///{tmp_path / 'inv_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine)

    def override_get_db():
        d = TS()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override_get_db
    db = TS()
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="龙牙", rule_system="coc", is_player=True, system_data={})
    ally = Character(name="健太", rule_system="coc", is_player=False, system_data={})
    db.add_all([module, hero, ally]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=hero.id, status="active", world_state={})
    db.add(s); db.flush()
    db.add_all([
        SessionParticipant(session_id=s.id, character_id=hero.id, role="human", is_primary=True,
                           owner_token=None, claimed=True, ready=True),
        SessionParticipant(session_id=s.id, character_id=ally.id, role="ai", is_primary=False,
                           claimed=True, ready=True),
    ])
    inv.add_item(db, hero, "火柴", qty=2, kind="consumable")
    inv.add_item(db, hero, "钥匙", kind="key")
    db.commit()
    sid, hid, aid = s.id, hero.id, ally.id
    # 取物品 id
    match_id = next(it["id"] for it in inv.get_inventory(hero) if it["name"] == "火柴")
    key_id = next(it["id"] for it in inv.get_inventory(hero) if it["name"] == "钥匙")
    db.close()

    try:
        c = TestClient(app)
        # 使用消耗品 → -1 + 落一条 pending_turn 动作
        assert c.post(f"/api/sessions/{sid}/inventory/use", json={"item_id": match_id}).status_code == 200
        r = c.get(f"/api/sessions/{sid}/inventory?char_id={hid}").json()
        assert next(it["qty"] for it in r["items"] if it["name"] == "火柴") == 1
        # 转让钥匙给队友
        assert c.post(f"/api/sessions/{sid}/inventory/give",
                      json={"item_id": key_id, "to_character_id": aid}).status_code == 200
        assert not any(it["name"] == "钥匙" for it in
                       c.get(f"/api/sessions/{sid}/inventory?char_id={hid}").json()["items"])
        assert any(it["name"] == "钥匙" for it in
                   c.get(f"/api/sessions/{sid}/inventory?char_id={aid}").json()["items"])
    finally:
        app.dependency_overrides.clear()
