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


def test_mechanical_chunks_carry_combat_log_flag(db_factory):
    """机械结算行（system）带 combat_log 供前端归入折叠日志抽屉；KP 叙述（narration_full）不带。"""
    import json
    db = db_factory()
    sid, _ = _seed(db)
    line = combat_service._combat_line(db, sid, "打手 受到 3 点伤害（HP 8→5）")
    ldata = json.loads(line.removeprefix("data: "))
    assert ldata["type"] == "system" and ldata["metadata"]["combat_log"] is True
    narr = combat_service._combat_narration(db, sid, "拳风掠过，血光四溅。")
    ndata = json.loads(narr.removeprefix("data: "))
    assert ndata["type"] == "narration_full" and "combat_log" not in ndata.get("metadata", {})


def test_combat_meta_order_carries_conditions_and_aim(db_factory):
    """_combat_meta 的 order 投影要透传 conditions 与 aim，供前端 HUD 渲染被擒/缴械/瞄准徽标。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 30}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    thug = combat_service._find(state, "npc_thug")
    thug["conditions"] = ["grappled", "disarmed"]
    hero_p = combat_service._find(state, hero.id)
    hero_p["aim"] = True
    meta = combat_service._combat_meta(state)
    by_id = {o["id"]: o for o in meta["order"]}
    assert by_id["npc_thug"]["conditions"] == ["grappled", "disarmed"]
    assert by_id["npc_thug"]["aim"] is False
    assert by_id[hero.id]["aim"] is True
    assert by_id[hero.id]["conditions"] == []


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


def test_dying_participant_ticks_each_round(db_factory, monkeypatch):
    """濒死 NPC 落在先攻序中：回合开始被体质 tick；CON 必失败 → 死亡 + 广播濒死检定行。"""
    db = db_factory()
    sid, hero = _seed(db)
    dying = {"id": "npc_dying", "name": "垂死者", "attributes": {"DEX": 95, "CON": 50, "SIZ": 50},
             "skills": {"格斗(斗殴)": 40}, "weapon": "徒手格斗"}
    guard = {"id": "npc_guard", "name": "卫兵", "attributes": {"DEX": 90, "CON": 60, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    # 垂死者 DEX95 先攻首位、卫兵次之、英雄末位；把垂死者置为濒死态（否则会先手攻击）
    state = combat_service.start_combat(
        db, sid,
        [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(dying, "enemy"),
         combat_service._npc_participant(guard, "enemy")])
    dp = combat_service._find(state, "npc_dying")
    dp["status"] = "dying"; dp["hp"] = 0
    combat_service._save_combat(db, sid, state)
    assert combat_service.current_actor(state)["id"] == "npc_dying"   # 垂死者先手（还有卫兵活着，战斗不结束）

    _fix_rolls(monkeypatch, [99, 80, 80], die=3)   # 濒死体质检定 99 必失败；卫兵随后攻英雄→暂停
    chunks, _ = asyncio.run(combat_service.drive_npcs(db, sid, state))

    dp = combat_service._find(state, "npc_dying")
    assert dp["status"] == "dead"                                      # 濒死体质检定失败 → 气绝
    assert any("濒死体质检定" in c for c in chunks)                      # 广播了濒死检定行


# ── P2：主动动作集（状态机分发 / apply_heal / 条件落库 / aim 消费）──────

def _seed_medic_hero(db):
    """建一个带急救/侦查/擒抱技能的英雄会话，返回 (sid, hero)。"""
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(
        name="医师", rule_system="coc", is_player=True,
        base_attributes={"DEX": 70, "CON": 60, "SIZ": 50},
        skills={"格斗(斗殴)": 60, "闪避": 35, "急救": 70, "侦查": 70, "射击(手枪)": 70},
        system_data={"hitPoints": {"current": 11, "max": 11}, "damageBonus": "0"},
    )
    db.add_all([module, hero]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=hero.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, hero


def test_apply_heal_writes_back_and_caps(db_factory):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    hp = combat_service._find(state, hero.id)
    hp["hp"] = 5   # 受伤
    # 回血 3 → 8
    combat_service.apply_heal(db, state, hp, 3)
    assert hp["hp"] == 8
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] == 8   # 写回角色卡
    # 回血封顶：max_hp=11，再回 99 只到 11
    combat_service.apply_heal(db, state, hp, 99)
    assert hp["hp"] == 11
    db.refresh(hero)
    assert hero.system_data["hitPoints"]["current"] == 11


def test_first_aid_heals_wounded_ally(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    ally = {"id": "npc_ally", "name": "同伴", "attributes": {"DEX": 30, "CON": 50, "SIZ": 50},
            "skills": {}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True),
                  combat_service._npc_participant(ally, "ally")],
        [])
    wounded = combat_service._find(state, "npc_ally")
    wounded["hp"] = 3; wounded["status"] = "major_wound"
    _fix_rolls(monkeypatch, [10])   # 急救70 → 成功
    actor = combat_service._find(state, hero.id)
    chunks, summary = combat_service._apply_one_action(
        db, sid, state, actor, {"type": "first_aid", "target_id": "npc_ally"})
    assert wounded["hp"] == 4                     # 回 1 HP
    assert wounded["first_aid_used"] is True       # 标记该处伤已急救
    assert summary


def test_first_aid_stabilizes_dying(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    ally = {"id": "npc_ally", "name": "同伴", "attributes": {"DEX": 30, "CON": 50, "SIZ": 50},
            "skills": {}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True),
                  combat_service._npc_participant(ally, "ally")],
        [])
    dying = combat_service._find(state, "npc_ally")
    dying["hp"] = 0; dying["status"] = "dying"
    _fix_rolls(monkeypatch, [10])   # 急救成功
    actor = combat_service._find(state, hero.id)
    combat_service._apply_one_action(db, sid, state, actor, {"type": "first_aid", "target_id": "npc_ally"})
    assert dying["status"] == "unconscious"        # 濒死稳住（稳定但出局）
    assert dying["first_aid_used"] is True


def test_first_aid_used_blocks_repeat(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    ally = {"id": "npc_ally", "name": "同伴", "attributes": {"DEX": 30, "CON": 50, "SIZ": 50},
            "skills": {}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True),
                  combat_service._npc_participant(ally, "ally")],
        [])
    wounded = combat_service._find(state, "npc_ally")
    wounded["hp"] = 3; wounded["status"] = "major_wound"; wounded["first_aid_used"] = True
    actor = combat_service._find(state, hero.id)
    chunks, summary = combat_service._apply_one_action(
        db, sid, state, actor, {"type": "first_aid", "target_id": "npc_ally"})
    assert wounded["hp"] == 3        # 该处伤已急救过，不再回血


def test_observe_produces_beat(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)], [])
    _fix_rolls(monkeypatch, [10])   # 侦查70 → 成功
    actor = combat_service._find(state, hero.id)
    chunks, summary = combat_service._apply_one_action(
        db, sid, state, actor, {"type": "observe"})
    assert summary and "观察" in summary


def test_maneuver_grapple_appends_condition(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 30, "闪避": 20}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    _fix_rolls(monkeypatch, [10, 80])   # 英雄格斗60成功、打手格斗30失败 → 英雄胜
    actor = combat_service._find(state, hero.id)
    combat_service._apply_one_action(
        db, sid, state, actor, {"type": "maneuver", "target_id": "npc_thug", "kind": "grapple"})
    thug = combat_service._find(state, "npc_thug")
    assert "grappled" in thug["conditions"]
    # 去重：再擒抱一次不重复追加
    _fix_rolls(monkeypatch, [10, 80])
    combat_service._apply_one_action(
        db, sid, state, actor, {"type": "maneuver", "target_id": "npc_thug", "kind": "grapple"})
    assert thug["conditions"].count("grappled") == 1


def test_aim_then_attack_consumes_flag(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    actor = combat_service._find(state, hero.id)
    # aim 置 flag
    combat_service._apply_one_action(db, sid, state, actor, {"type": "aim"})
    assert actor["aim"] is True
    # 随后攻击应消费 aim（打完清标记）
    _fix_rolls(monkeypatch, [10, 80], die=3)
    combat_service._apply_one_action(
        db, sid, state, actor, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})
    assert actor["aim"] is False


def test_disarmed_attacker_forced_unarmed(db_factory, monkeypatch):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    actor = combat_service._find(state, hero.id)
    actor["conditions"].append("disarmed")
    _fix_rolls(monkeypatch, [10, 80], die=3)   # 命中
    chunks, summary = combat_service._apply_one_action(
        db, sid, state, actor, {"type": "attack", "target_id": "npc_thug", "weapon": "手枪"})
    # 被缴械 → 即使指定手枪也按徒手格斗结算
    assert "徒手格斗" in summary


def test_reload_marks_loaded(db_factory):
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)], [])
    actor = combat_service._find(state, hero.id)
    chunks, summary = combat_service._apply_one_action(
        db, sid, state, actor, {"type": "reload"})
    assert actor.get("loaded") is True and summary


def test_grappled_human_reaction_only_fight_back(db_factory):
    """被擒抱的真人被 NPC 攻击时，pending_reaction.allowed 只剩反击。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    hero_p = combat_service._find(state, hero.id)
    hero_p["conditions"].append("grappled")   # 真人已被擒抱
    combat_service._save_combat(db, sid, state)
    asyncio.run(combat_service.drive_npcs(db, sid, state))
    pr = state.get("pending_reaction")
    assert pr and pr["defender_id"] == hero.id
    assert pr["allowed"] == ["fight_back"]   # 被擒抱近战只能反击


def test_grappled_npc_defender_forced_fight_back(db_factory, monkeypatch):
    """C 审查 I2：玩家擒抱 NPC 后近战攻击，NPC 防御被收窄到反击（不能闪避）——
    与真人路径对称。用 monkeypatch 捕获传给 resolve_attack 的 defense。"""
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 60}, "weapon": "徒手格斗", "combat_ai": "cautious"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    thug = combat_service._find(state, "npc_thug")
    thug["conditions"].append("grappled")   # NPC 已被擒抱

    captured = {}
    real = combat_service.engine.resolve_attack

    def _spy(*args, **kwargs):
        captured["defense"] = kwargs.get("defense")
        return real(*args, **kwargs)
    monkeypatch.setattr(combat_service.engine, "resolve_attack", _spy)

    _fix_rolls(monkeypatch, [10, 80], die=3)
    actor = combat_service._find(state, hero.id)
    combat_service._apply_one_action(
        db, sid, state, actor, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})
    assert captured["defense"] == "fight_back"   # 被擒抱的 NPC 不能闪避，强制反击


def test_first_aid_used_resets_on_new_wound_then_stabilizes(db_factory, monkeypatch):
    """C 审查 C1：同伴先被急救标 used → 再受伤到 dying → first_aid 仍能稳住濒死。"""
    db = db_factory()
    sid, hero = _seed_medic_hero(db)
    ally = {"id": "npc_ally", "name": "同伴", "attributes": {"DEX": 30, "CON": 50, "SIZ": 50},
            "skills": {}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True),
                  combat_service._npc_participant(ally, "ally")],
        [])
    wounded = combat_service._find(state, "npc_ally")
    wounded["hp"] = 5; wounded["first_aid_used"] = True   # 前期已被急救过

    # 再受一次伤，直接打到 0（dying）——apply_damage 应把 first_aid_used 重置
    combat_service.apply_damage(db, state, wounded, 5, reason="被重击")
    assert wounded["status"] == "dying"
    assert wounded["first_aid_used"] is False   # C1：新伤重置了急救标记

    # 现在急救应能稳住濒死（而非被顶端 used 检查拒绝）
    _fix_rolls(monkeypatch, [10])   # 急救70 成功
    actor = combat_service._find(state, hero.id)
    combat_service._apply_one_action(
        db, sid, state, actor, {"type": "first_aid", "target_id": "npc_ally"})
    assert wounded["status"] == "unconscious"   # dying → unconscious 稳住
    assert wounded["first_aid_used"] is True


def test_reaction_disarmed_attacker_uses_unarmed(db_factory, monkeypatch):
    """M3：pending 的攻方 conditions 含 disarmed → 反应结算时攻击强制徒手。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_gun", "name": "枪手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"射击(手枪)": 55, "格斗(斗殴)": 40, "闪避": 20}, "weapon": "自动手枪"}
    state = _paused_by_npc_attack(db, sid, hero, enemy)
    attacker = combat_service._find(state, "npc_gun")
    attacker["conditions"].append("disarmed")   # 枪手已被缴械
    combat_service._save_combat(db, sid, state)

    captured = {}
    real = combat_service.engine.resolve_attack

    def _spy(*args, **kwargs):
        r = real(*args, **kwargs)
        captured["disarmed"] = kwargs.get("attacker_disarmed")
        captured["weapon"] = r["weapon"]
        return r
    monkeypatch.setattr(combat_service.engine, "resolve_attack", _spy)

    # 火器 pending 但攻方被缴械：反应结算走徒手；choice 用 dodge（火器 allowed 含 dodge）
    _fix_rolls(monkeypatch, [50, 90], die=3)
    asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "dodge"))
    assert captured["disarmed"] is True
    assert captured["weapon"] == "徒手格斗"   # 缴械后强制徒手结算
