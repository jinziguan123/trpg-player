"""急救/医学：规则固定治疗效果的确定性结算（检定成功即引擎回血，不靠 KP 自觉发 HP_CHANGE）。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service as cs


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fa.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, hp, max_hp=11, status="active", flag=False):
    sd = {"hitPoints": {"current": hp, "max": max_hp}}
    if flag:
        sd["firstAidUsed"] = True
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="伊芙琳", rule_system="coc", is_player=True, system_data=sd, status=status)
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, pc


def _hp(pc):
    return pc.system_data["hitPoints"]["current"]


def test_heal_kind_maps_skills():
    assert cs._heal_kind("急救") == "first_aid"
    assert cs._heal_kind("医学") == "medicine"
    assert cs._heal_kind("侦查") is None


def test_first_aid_heals_one_on_success(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=8)
    chunks = cs._apply_heal_on_success(db, sid, pc, "急救", "success")
    assert chunks and _hp(pc) == 9
    assert pc.system_data.get("firstAidUsed") is True


def test_no_heal_on_failure(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=8)
    assert cs._apply_heal_on_success(db, sid, pc, "急救", "failure") == []
    assert _hp(pc) == 8 and not pc.system_data.get("firstAidUsed")


def test_non_heal_skill_ignored(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=8)
    assert cs._apply_heal_on_success(db, sid, pc, "侦查", "success") == []
    assert _hp(pc) == 8


def test_first_aid_stabilizes_dying(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=0, status="dying")
    cs._apply_heal_on_success(db, sid, pc, "急救", "hard_success")
    assert _hp(pc) == 1 and pc.status == "active"   # 濒死稳住 + 唤醒


def test_once_per_wound_blocks_second(db_factory):
    db = db_factory(); sid, pc = _seed(db, hp=8, flag=True)
    chunks = cs._apply_heal_on_success(db, sid, pc, "急救", "success")
    assert chunks and _hp(pc) == 8   # 已处理过 → 不再叠加，HP 不变


def test_medicine_heals_1d3(db_factory, monkeypatch):
    db = db_factory(); sid, pc = _seed(db, hp=5)
    monkeypatch.setattr(cs.random, "randint", lambda a, b: 3)
    cs._apply_heal_on_success(db, sid, pc, "医学", "success")
    assert _hp(pc) == 8   # 5 + 1D3(=3)


def test_new_damage_clears_first_aid_flag(db_factory):
    """受新伤 = 新的急救机会：_exec_hp_change 扣血时清 firstAidUsed。"""
    db = db_factory(); sid, pc = _seed(db, hp=8, flag=True)
    asyncio.run(cs._exec_hp_change(db, sid, pc, "player", "-3", "被击中"))
    db.refresh(pc)
    assert pc.system_data.get("firstAidUsed") is False and _hp(pc) == 5
    # 清零后可再次成功急救
    cs._apply_heal_on_success(db, sid, pc, "急救", "success")
    assert _hp(pc) == 6
