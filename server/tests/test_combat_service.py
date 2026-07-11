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


def test_reject_action_out_of_turn(db_factory):
    db = db_factory()
    sid, hero = _seed(db)
    # 敌方 DEX 低于英雄 → 先攻首位是英雄（current_actor 为真人），战斗停在英雄回合
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state, _ = _start(db, sid, hero, enemy)
    assert combat_service.current_actor(state)["id"] == hero.id   # 当前是英雄回合
    with pytest.raises(ValueError, match="先攻回合"):
        _act(db, sid, "npc_thug", {"type": "dodge"})   # 不是打手回合 → 拒绝


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


def test_combat_result_folds_into_kp_context(db_factory):
    """战斗结果摘要注入主 KP 上下文（只给结论、供续写余波）。"""
    from app.ai.context import build_kp_context
    db = db_factory()
    sid, hero = _seed(db)
    session = db.get(GameSession, sid)
    module = db.get(Module, session.module_id)
    ws = dict(session.world_state or {})
    ws["combat_result"] = {"outcome": "players_win", "rounds": 3,
                           "casualties": [{"name": "打手", "status": "dead"}],
                           "hp_after": {"伊芙琳": 8}}
    session.world_state = ws
    db.commit()

    sys = build_kp_context(session, module, hero, [])[0]["content"]
    assert "刚结束的交战" in sys and "调查员一方获胜" in sys and "打手" in sys


def test_key_npc_uses_agent_decision(db_factory):
    """有性格的关键 NPC 走子代理决策：agent.decide 选定攻击真人 → 决策仍驱动这次攻击，
    但攻击真人现在路由到交互暂停（落 pending_reaction + 广播 combat_reaction_prompt），
    伤害/叙述归 resolve_reaction 结算（另见其专测），此处不在暂停时结算或调 narrate。"""
    db = db_factory()
    sid, hero = _seed(db)
    boss = {"id": "npc_boss", "name": "祭司", "attributes": {"DEX": 90, "CON": 50, "SIZ": 50},
            "skills": {"格斗(斗殴)": 55, "闪避": 30}, "weapon": "徒手格斗",
            "personality": "冷酷、优先解决最强者"}

    class _Agent:
        def __init__(self): self.decided = False; self.narrated = False
        async def decide(self, state, npc, scene_hint=""):
            self.decided = True
            return {"action": "attack", "target_id": hero.id, "weapon": "徒手格斗"}
        async def narrate(self, state, beats, scene_hint=""):
            self.narrated = True
            return "祭司狞笑着扑向伊芙琳。"

    agent = _Agent()
    state, chunks = asyncio.run(combat_service.start(
        db, sid, [hero], [boss], {hero.id}, agent=agent))
    assert agent.decided is True                              # 关键 NPC 走了子代理决策
    assert any('"combat_reaction_prompt"' in c for c in chunks)   # 决策的攻击路由到交互暂停
    pr = state.get("pending_reaction")
    assert pr and pr["attacker_id"] == "npc_boss" and pr["defender_id"] == hero.id
    assert agent.narrated is False                            # 暂停时不叙述（叙述归 resolve_reaction）
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] == 11     # 暂停期间未结算伤害


def test_drive_npcs_pauses_when_npc_attacks_human(db_factory):
    """NPC（先攻在真人之前）启发式攻击真人时，驱动应暂停：落 pending_reaction、广播提示、不结算伤害。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    # 直接建战斗态并把指针置于打手（先攻首位，DEX90>70），跑 drive_npcs
    state = combat_service.start_combat(
        db, sid,
        [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    assert combat_service.current_actor(state)["id"] == "npc_thug"   # 打手先手
    hp_before = hero.system_data["hitPoints"]["current"]

    chunks, beats = asyncio.run(combat_service.drive_npcs(db, sid, state))

    pr = state.get("pending_reaction")
    assert pr and pr["defender_id"] == hero.id and pr["attacker_id"] == "npc_thug"
    assert pr["allowed"] == ["fight_back", "dodge"]                  # 徒手非火器
    assert any('"combat_reaction_prompt"' in c for c in chunks)      # 广播了反应提示
    # 未结算伤害：真人 HP 不变，pending 期间无骰子结算
    hero_p = combat_service._find(state, hero.id)
    assert hero_p["hp"] == hp_before
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] == hp_before


def _paused_by_npc_attack(db, sid, hero, enemy):
    """建战斗态、驱动到「NPC 攻击真人」暂停点，返回 state（含 pending_reaction）。"""
    state = combat_service.start_combat(
        db, sid,
        [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    asyncio.run(combat_service.drive_npcs(db, sid, state))
    assert state.get("pending_reaction")   # 已停在等真人反应
    return state


def test_resolve_reaction_dodge_success_no_damage_to_attacker(db_factory, monkeypatch):
    """核心 ⑤ 回归：真人闪避成功、攻方失手 → 攻击者 HP 绝不变、pending 清空、轮次已推进。"""
    db = db_factory()
    sid, hero = _seed(db)   # 英雄 DEX70 闪避35
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state = _paused_by_npc_attack(db, sid, hero, enemy)
    # 打手(DEX90)是先攻首位、英雄次之；结算后指针从打手推进到英雄（本轮内，round 不变）
    assert combat_service.current_actor(state)["id"] == "npc_thug"
    attacker = combat_service._find(state, "npc_thug")
    atk_hp_before = attacker["hp"]

    # 结算这一击：攻方掷 80（>45 失手）、真人闪避掷 10（<35 成功）→ 攻方成功等级不高于守方 → 未命中
    _fix_rolls(monkeypatch, [80, 10], die=3)
    out = asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "dodge"))

    st = combat_service.get_combat(db.get(GameSession, sid))
    atk = next(p for p in st["initiative"] if p["id"] == "npc_thug")
    assert atk["hp"] == atk_hp_before                 # 闪避永不伤攻击者（⑤ 核心）
    hero_p = next(p for p in st["initiative"] if p["id"] == hero.id)
    assert hero_p["hp"] == 11                          # 闪开了，真人也没掉血
    assert st.get("pending_reaction") is None          # pending 已清空
    assert combat_service.current_actor(st)["id"] == hero.id   # 攻方已行动 → 推进到英雄回合
    assert any('"dice"' in c for c in out)             # 结算广播了骰子


def test_resolve_reaction_rejects_wrong_defender(db_factory):
    """非 pending 的防御者提交反应 → raise ValueError。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    _paused_by_npc_attack(db, sid, hero, enemy)   # pending 的 defender 是 hero
    with pytest.raises(ValueError, match="等待你的反应"):
        asyncio.run(combat_service.resolve_reaction(db, sid, "npc_thug", "dodge"))


def test_resolve_reaction_rejects_disallowed_choice(db_factory):
    """火器 pending 下提交 fight_back（不在 allowed）→ raise ValueError。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_gun", "name": "枪手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"射击(手枪)": 55, "闪避": 20}, "weapon": "自动手枪"}
    state = _paused_by_npc_attack(db, sid, hero, enemy)
    assert state["pending_reaction"]["ranged"] is True
    assert "fight_back" not in state["pending_reaction"]["allowed"]   # 火器不能反击
    with pytest.raises(ValueError, match="不可用"):
        asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "fight_back"))
