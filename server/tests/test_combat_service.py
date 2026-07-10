"""战斗状态机（P2）单测：起战斗 / 先攻停在玩家 / 玩家行动 / NPC 自动 / HP 应用 / 结束摘要。

掷骰经 monkeypatch 钉死；不接 LLM、不接前端。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module
from app.services import combat_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'c.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seq(values):
    it = iter(values)
    return lambda: next(it)


def _fix_rolls(monkeypatch, d100_seq, die=3):
    """钉死 d100 序列（检定）与武器骰点（伤害）。"""
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq(d100_seq))
    monkeypatch.setattr("app.rules.coc.combat.random.randint", lambda a, b: die)


def _seed(db):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(
        name="伊芙琳", rule_system="coc", is_player=True,
        base_attributes={"DEX": 70, "CON": 60, "SIZ": 50},
        skills={"格斗(斗殴)": 60, "闪避": 35},
        system_data={"hitPoints": {"current": 11, "max": 11}, "damageBonus": "0"},
    )
    db.add_all([module, hero]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=hero.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, hero


def test_start_pauses_at_human_and_broadcasts_state(db_factory):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    state, chunks = combat_service.start(db, sid, [hero], [enemy], {hero.id}, trigger="打手扑上来")
    actor = combat_service.current_actor(state)
    assert actor["id"] == hero.id and actor["is_human"] is True    # DEX70>40，先攻首位是英雄
    assert any('"combat_start"' in c for c in chunks)


def test_player_attack_hits_and_npc_counterattacks(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    combat_service.start(db, sid, [hero], [enemy], {hero.id})
    # 英雄攻(10命中级高) / 打手闪避(80失败) → 命中；打手回合攻(10) / 英雄防守(80失败) → 挨打
    _fix_rolls(monkeypatch, [10, 80, 10, 80], die=3)
    chunks = combat_service.resolve_player_action(
        db, sid, hero.id, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})

    st = combat_service.get_combat(db.get(GameSession, sid))
    thug = next(p for p in st["initiative"] if p["id"] == "npc_thug")
    assert thug["hp"] < thug["max_hp"]                       # 打手挨打掉血
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] <= 11    # 英雄被反击也掉血/同步角色卡
    assert any('"dice"' in c for c in chunks)


def test_reject_action_out_of_turn(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    # 打手 DEX90 > 英雄 → 打手先手，会自动打英雄一下再停在英雄回合
    _fix_rolls(monkeypatch, [10, 80], die=1)  # 打手攻中、英雄防守失败
    combat_service.start(db, sid, [hero], [enemy], {hero.id})
    # 用一个不存在的行动者 id 提交 → 拒绝
    with pytest.raises(ValueError, match="先攻回合"):
        combat_service.resolve_player_action(db, sid, "npc_thug", {"type": "dodge"})


def test_combat_ends_when_enemy_down(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_weak", "name": "虚弱者", "attributes": {"DEX": 30, "CON": 10, "SIZ": 10},
             "skills": {"格斗(斗殴)": 20, "闪避": 10}, "weapon": "徒手格斗"}
    combat_service.start(db, sid, [hero], [enemy], {hero.id})
    _fix_rolls(monkeypatch, [1, 90, 90], die=8)   # 英雄大成功命中、伤害拉满，秒掉 HP=2 的敌人
    chunks = combat_service.resolve_player_action(
        db, sid, hero.id, {"type": "attack", "target_id": "npc_weak", "weapon": "大棒(棒球棒、拨火棍)"})

    session = db.get(GameSession, sid)
    assert combat_service.get_combat(session) is None                             # 战斗态已清
    assert session.world_state.get("combat_result", {}).get("outcome") == "players_win"
    assert any('"combat_end"' in c for c in chunks)
