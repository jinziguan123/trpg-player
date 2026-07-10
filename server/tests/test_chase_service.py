"""追逐状态机（P5）单测：起追逐 / 逐轮推 gap / 脱身&被追上折回。掷骰钉死，agent=None。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module
from app.services import chase_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'ch.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seq(values):
    it = iter(values)
    return lambda: next(it)


def _seed(db):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="伊芙琳", rule_system="coc", is_player=True,
                     base_attributes={"DEX": 60}, skills={"运动": 60},
                     system_data={"move": 8})
    db.add_all([module, hero]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=hero.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, hero


def _start(db, sid, hero, **kw):
    q = chase_service._quarry_from_char(hero)
    p = chase_service._pursuer_from_npc({"name": "追兵", "attributes": {"DEX": 55},
                                         "skills": {"运动": 55}, "mov": 8})
    return chase_service.start_chase(db, sid, q, p, **kw)


def test_quarry_wins_round_opens_gap(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    _start(db, sid, hero, escape_at=5, caught_at=-3)
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([45, 80]))  # 逃成功、追失败
    asyncio.run(chase_service.resolve_chase_round(db, sid, {"type": "run"}))
    st = chase_service.get_chase(db.get(GameSession, sid))
    assert st["gap"] == 1 and st["round"] == 1


def test_escape_folds_result(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    _start(db, sid, hero, escape_at=2, caught_at=-3)     # 阈值调小，易脱身
    # 连续两轮逃方大成功（+2）→ gap 达 2 → 脱身
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([10, 80]))
    chunks = asyncio.run(chase_service.resolve_chase_round(db, sid, {"type": "run"}))
    session = db.get(GameSession, sid)
    assert chase_service.get_chase(session) is None                       # 追逐态已清
    assert session.world_state.get("combat_result", {}).get("outcome") == "escaped"
    assert any('"chase_end"' in c for c in chunks)


def test_caught_folds_result(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    _start(db, sid, hero, escape_at=5, caught_at=-1)     # 一轮被追上
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([80, 10]))  # 逃失败、追大成功(-2)
    asyncio.run(chase_service.resolve_chase_round(db, sid, {"type": "run"}))
    session = db.get(GameSession, sid)
    assert chase_service.get_chase(session) is None
    assert session.world_state.get("combat_result", {}).get("outcome") == "caught"
