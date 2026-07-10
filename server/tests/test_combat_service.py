"""战斗状态机（P2/P3）单测：起战斗 / 先攻停在玩家 / 玩家行动 / NPC 自动 / HP 应用 / 结束摘要。

掷骰经 monkeypatch 钉死；agent=None（纯机械结算，不接 LLM）。
"""

import asyncio

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


def _start(db, sid, hero, enemy, trigger=""):
    return asyncio.run(combat_service.start(db, sid, [hero], [enemy], {hero.id}, trigger=trigger))


def _act(db, sid, actor_id, action):
    return asyncio.run(combat_service.resolve_player_action(db, sid, actor_id, action))


def test_start_pauses_at_human_and_broadcasts_state(db_factory):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    state, chunks = _start(db, sid, hero, enemy, trigger="打手扑上来")
    actor = combat_service.current_actor(state)
    assert actor["id"] == hero.id and actor["is_human"] is True    # DEX70>40，先攻首位是英雄
    assert any('"combat_start"' in c for c in chunks)


def test_player_attack_hits_and_npc_counterattacks(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    _start(db, sid, hero, enemy)
    _fix_rolls(monkeypatch, [10, 80, 10, 80], die=3)   # 英雄攻中/打手闪避失败；打手攻中/英雄防守失败
    chunks = _act(db, sid, hero.id, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})

    st = combat_service.get_combat(db.get(GameSession, sid))
    thug = next(p for p in st["initiative"] if p["id"] == "npc_thug")
    assert thug["hp"] < thug["max_hp"]
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] <= 11
    assert any('"dice"' in c for c in chunks)


def test_reject_action_out_of_turn(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    _fix_rolls(monkeypatch, [10, 80], die=1)   # 打手 DEX90 先手、自动打英雄一下再停在英雄回合
    _start(db, sid, hero, enemy)
    with pytest.raises(ValueError, match="先攻回合"):
        _act(db, sid, "npc_thug", {"type": "dodge"})   # 不是打手回合了 → 拒绝


def test_combat_ends_when_enemy_down(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_weak", "name": "虚弱者", "attributes": {"DEX": 30, "CON": 10, "SIZ": 10},
             "skills": {"格斗(斗殴)": 20, "闪避": 10}, "weapon": "徒手格斗"}
    _start(db, sid, hero, enemy)
    _fix_rolls(monkeypatch, [1, 90, 90], die=8)   # 英雄大成功命中、伤害拉满，秒掉 HP=2 的敌人
    chunks = _act(db, sid, hero.id, {"type": "attack", "target_id": "npc_weak", "weapon": "大棒(棒球棒、拨火棍)"})

    session = db.get(GameSession, sid)
    assert combat_service.get_combat(session) is None
    assert session.world_state.get("combat_result", {}).get("outcome") == "players_win"
    assert any('"combat_end"' in c for c in chunks)


def test_key_npc_uses_agent_decision(db_factory, monkeypatch):
    """有性格的关键 NPC 走子代理决策：agent.decide 指定攻击目标即被采用。"""
    db = db_factory()
    sid, hero = _seed(db)
    boss = {"id": "npc_boss", "name": "祭司", "attributes": {"DEX": 90, "CON": 50, "SIZ": 50},
            "skills": {"格斗(斗殴)": 55, "闪避": 30}, "weapon": "徒手格斗",
            "personality": "冷酷、优先解决最强者"}

    class _Agent:
        def __init__(self): self.decided = False
        async def decide(self, state, npc, scene_hint=""):
            self.decided = True
            return {"action": "attack", "target_id": hero.id, "weapon": "徒手格斗"}
        async def narrate(self, state, beats, scene_hint=""):
            return "祭司狞笑着扑向伊芙琳。"

    agent = _Agent()
    _fix_rolls(monkeypatch, [10, 80], die=2)   # 祭司先手(DEX90)攻中、英雄防守失败
    state, chunks = asyncio.run(combat_service.start(
        db, sid, [hero], [boss], {hero.id}, agent=agent))
    assert agent.decided is True                 # 关键 NPC 走了子代理决策
    assert any('"narration_full"' in c for c in chunks)   # 有子代理叙述
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] < 11  # 祭司确实按决策打了英雄
