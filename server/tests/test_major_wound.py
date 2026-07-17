"""重伤确定性规则钩子的回归测试（CoC 7e：单次伤害 ≥ 最大 HP 一半 → 重伤，
自动过体质检定，失败昏迷）。不依赖真实 LLM，掷骰用 monkeypatch 钉死。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db) -> tuple[Module, Character, str]:
    module = Module(title="重伤测试", rule_system="coc", npcs=[])
    char = Character(
        name="亨利",
        rule_system="coc",
        base_attributes={"CON": 60},
        skills={},
        system_data={"hitPoints": {"current": 10, "max": 10}},
        status="active",
    )
    db.add_all([module, char])
    db.flush()
    session = GameSession(module_id=module.id, player_character_id=char.id, status="active")
    db.add(session)
    db.commit()
    return module, char, session.id


def _run_hp_change(db, module, char, session_id, delta: str):
    return asyncio.run(chat_service._exec_hp_change(
        db, session_id, char, "player", delta, "测试伤害", module=module,
    ))


def _events(db, session_id):
    return session_service.get_session_events(db, session_id)


def test_重伤且体质检定失败则昏迷(db_factory, monkeypatch):
    db = db_factory()
    module, char, sid = _seed(db)
    # 钉死掷骰为 99 → 必失败（CON 60）
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 99)

    chunks = _run_hp_change(db, module, char, sid, "-5")  # 5 ≥ 10//2 → 重伤

    assert char.status == "unconscious"
    dice = [e for e in _events(db, sid) if e.event_type == "dice"]
    assert len(dice) == 1
    assert dice[0].metadata_.get("major_wound_check") is True
    assert "昏迷倒地" in dice[0].content
    # HP 结算 + 体质检定两条（character_update 刷新信号不计）
    assert len([c for c in chunks if "character_update" not in c]) == 2


def test_重伤但体质检定成功保持清醒(db_factory, monkeypatch):
    db = db_factory()
    module, char, sid = _seed(db)
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 30)  # 30 ≤ 60 成功

    _run_hp_change(db, module, char, sid, "-5")

    assert char.status == "major_wound"  # 重伤但未昏迷
    dice = [e for e in _events(db, sid) if e.event_type == "dice"]
    assert len(dice) == 1 and "昏迷倒地" not in dice[0].content


def test_轻伤不触发体质检定(db_factory):
    db = db_factory()
    module, char, sid = _seed(db)

    _run_hp_change(db, module, char, sid, "-2")  # 2 < 10//2

    assert char.status == "active"
    assert not [e for e in _events(db, sid) if e.event_type == "dice"]


def test_伤害致零直接濒死不再过体质(db_factory):
    # HP 归零走濒死路径（需急救），不做「昏迷判定」——濒死已是更重的状态
    db = db_factory()
    module, char, sid = _seed(db)

    chunks = _run_hp_change(db, module, char, sid, "-10")

    assert not [e for e in _events(db, sid) if e.event_type == "dice"]
    assert "濒死" in _events(db, sid)[0].content
    assert len([c for c in chunks if "character_update" not in c]) == 1


def test_恢复不触发(db_factory):
    db = db_factory()
    module, char, sid = _seed(db)

    _run_hp_change(db, module, char, sid, "3")

    assert char.status == "active"
    assert not [e for e in _events(db, sid) if e.event_type == "dice"]


def test_无module时向后兼容不检定(db_factory):
    db = db_factory()
    module, char, sid = _seed(db)

    chunks = asyncio.run(chat_service._exec_hp_change(
        db, sid, char, "player", "-5", "测试",
    ))

    # 只有 HP 结算，无检定（module 缺省 → 与旧行为一致）；character_update 刷新信号不计
    assert len([c for c in chunks if "character_update" not in c]) == 1
    assert char.status == "active"


# ── 队友 HP 结算 ──

def _ally(db, name="阿尔法", con=60, hp=10):
    a = Character(
        name=name, rule_system="coc", is_player=False,
        base_attributes={"CON": con}, skills={},
        system_data={"hitPoints": {"current": hp, "max": hp}}, status="active",
    )
    db.add(a); db.commit()
    return a


def test_队友受伤也结算并重伤昏迷(db_factory, monkeypatch):
    db = db_factory()
    module, hero, sid = _seed(db)
    ally = _ally(db)
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: 99)  # 体质必失败→昏迷
    asyncio.run(chat_service._exec_hp_change(
        db, sid, hero, "阿尔法", "-5", "被兽爪撕开", module=module, teammates=[ally],
    ))
    assert ally.system_data["hitPoints"]["current"] == 5   # 队友 HP 结算
    assert ally.status == "unconscious"                    # 队友也会重伤昏迷
    assert hero.status == "active"                         # 主角不受影响
    assert any(e.metadata_.get("actor") == "阿尔法" for e in _events(db, sid) if e.event_type == "system")


def test_未知target不结算(db_factory):
    db = db_factory()
    module, hero, sid = _seed(db)
    chunks = asyncio.run(chat_service._exec_hp_change(
        db, sid, hero, "路人甲", "-5", "", module=module, teammates=[],
    ))
    assert chunks == []                                    # NPC/匹配不到 → 不结算


# ── SAN 疯狂落状态字段 ──

def test_san归零落永久疯狂(db_factory):
    db = db_factory()
    module, hero, sid = _seed(db)
    r = chat_service._apply_madness_status(db, hero, new_san=0, went_insane=False)
    assert r == "permanent_insanity" and hero.status == "permanent_insanity"


def test_大额损失落临时疯狂(db_factory):
    db = db_factory()
    module, hero, sid = _seed(db)
    r = chat_service._apply_madness_status(db, hero, new_san=40, went_insane=True)
    assert r == "temporary_insanity" and hero.status == "temporary_insanity"


def test_疯狂不降级更严重状态(db_factory):
    db = db_factory()
    module, hero, sid = _seed(db)
    # 已昏迷：临时疯狂（severity 2）< 昏迷（4）→ 不降级
    hero.status = "unconscious"; db.add(hero); db.commit()
    assert chat_service._apply_madness_status(db, hero, 40, True) is None
    assert hero.status == "unconscious"
    # 已永久疯狂：不被临时疯狂降级
    hero.status = "permanent_insanity"; db.add(hero); db.commit()
    assert chat_service._apply_madness_status(db, hero, 40, True) is None
    assert hero.status == "permanent_insanity"


def test_未疯狂不改状态(db_factory):
    db = db_factory()
    module, hero, sid = _seed(db)
    assert chat_service._apply_madness_status(db, hero, 40, False) is None
    assert hero.status == "active"
