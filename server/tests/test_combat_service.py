"""战斗状态机（P2/P3）单测：起战斗 / 先攻停在玩家 / 玩家行动 / NPC 自动 / HP 应用 / 结束摘要。

掷骰经 monkeypatch 钉死；agent=None（纯机械结算，不接 LLM）。
"""

import asyncio
import json

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


def _roll(db, sid, actor_id):
    """两段式攻击第二段：玩家亲自掷伤害。"""
    return asyncio.run(combat_service.resolve_combat_roll(db, sid, actor_id))


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


def _start_multi(db, sid, hero, enemies):
    return combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(e, "enemy") for e in enemies])


def _mk_enemy(eid, name):
    return {"id": eid, "name": name, "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
            "skills": {"格斗(斗殴)": 30}, "weapon": "徒手格斗"}


def _cluster_adjacent(db, sid, state):
    """把参战方摆成一小簇相邻（覆盖拉开布阵）——供「NPC 攻击真人→暂停」类测试绕开走位接近，
    让 NPC 开局就够得着、直接落 pending_reaction。"""
    spots = [(5, 5), (6, 5), (5, 6), (6, 6), (6, 4), (4, 5)]
    for p, (x, y) in zip(state.get("initiative") or [], spots):
        p["pos"] = {"x": x, "y": y}
    combat_service._save_combat(db, sid, state)


def test_stage_aoe_damage_molotov_targets_enemies_and_burns(db_factory, monkeypatch):
    """燃烧弹：查武器表得 2D6+烧，挂成投掷者 pending_roll，波及多个敌人、附燃烧。"""
    db = db_factory(); sid, hero = _seed(db)
    _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A"), _mk_enemy("e2", "循声者B")])
    _fix_rolls(monkeypatch, [], die=4)   # 伤害骰=4 → 2D6=8
    chunk, staged = combat_service.stage_aoe_damage(
        db, sid, hero.id, ["循声者A", "循声者B"], weapon="莫洛托夫鸡尾酒", dedup_key="k1")
    assert staged and chunk
    pr = combat_service.get_combat(db.get(GameSession, sid))["pending_roll"]
    assert pr["kind"] == "aoe_damage" and pr["actor_id"] == hero.id
    assert set(pr["victim_ids"]) == {"e1", "e2"}
    assert pr["burning"] is True and pr["damage"]["total"] == 8


def test_stage_aoe_dedup_and_no_targets(db_factory, monkeypatch):
    db = db_factory(); sid, hero = _seed(db)
    _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    _fix_rolls(monkeypatch, [], die=3)
    # 匹配不到目标 → 不挂
    _, staged = combat_service.stage_aoe_damage(db, sid, hero.id, ["不存在"], formula="2d6")
    assert not staged
    # 挂一次
    _, s1 = combat_service.stage_aoe_damage(db, sid, hero.id, ["循声者A"], formula="2d6", dedup_key="k")
    assert s1
    # 清掉 pending_roll 后同 dedup_key 再挂 → 幂等跳过
    st = combat_service.get_combat(db.get(GameSession, sid)); st["pending_roll"] = None
    combat_service._save_combat(db, sid, st)
    _, s2 = combat_service.stage_aoe_damage(db, sid, hero.id, ["循声者A"], formula="2d6", dedup_key="k")
    assert not s2


def test_resolve_aoe_applies_to_all_and_burns_without_advancing(db_factory, monkeypatch):
    db = db_factory(); sid, hero = _seed(db)
    _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A"), _mk_enemy("e2", "循声者B")])
    _fix_rolls(monkeypatch, [], die=2)   # 2D6=4（< 半血，不触发重伤 CON 检定，无需 d100）
    combat_service.stage_aoe_damage(db, sid, hero.id, ["循声者A", "循声者B"],
                                    weapon="莫洛托夫鸡尾酒", dedup_key="k1")
    turn_before = combat_service.get_combat(db.get(GameSession, sid))["turn_index"]
    _roll(db, sid, hero.id)   # resolve_combat_roll → aoe 分支
    st = combat_service.get_combat(db.get(GameSession, sid))
    e1, e2 = combat_service._find(st, "e1"), combat_service._find(st, "e2")
    assert e1["hp"] < e1["max_hp"] and e2["hp"] < e2["max_hp"]      # 两个都扣了血
    assert "burning" in e1["conditions"] and "burning" in e2["conditions"]
    assert not st.get("pending_roll")
    assert st["turn_index"] == turn_before                          # AoE 不推进先攻


def test_fight_back_counter_is_player_rolled(db_factory, monkeypatch):
    """反击命中攻方后，反击伤害不再自动结算——挂成防守玩家的 pending_roll，由其亲手掷。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    # 手动设 pending_reaction：e1 徒手攻击 hero（模拟 NPC 攻击真人后的暂停点）。
    state["pending_reaction"] = {
        "attacker_id": "e1", "defender_id": hero.id, "attacker_name": "循声者A",
        "defender_name": hero.name, "weapon": "徒手格斗", "ranged": False,
        "allowed": ["fight_back", "dodge"],
    }
    combat_service._save_combat(db, sid, state)
    # 反击对抗：e1 格斗90失败、hero 格斗5成功 → hero 胜 → 反击命中攻方（damage_to=attacker）。
    _fix_rolls(monkeypatch, [90, 5], die=2)   # 反击伤害 1D3(die=2)=2 < 半血，不触发 CON 检定
    out = asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "fight_back"))
    assert any('"combat_state"' in c for c in out)
    st = combat_service.get_combat(db.get(GameSession, sid))
    pr = st["pending_roll"]
    assert pr and pr["kind"] == "damage" and pr["actor_id"] == hero.id and pr["victim_id"] == "e1"
    assert pr.get("no_advance") is True
    e1 = combat_service._find(st, "e1")
    assert e1["hp"] == e1["max_hp"]                 # 反击伤害尚未结算，等玩家掷
    # 玩家亲手掷反击伤害 → e1 扣血
    _roll(db, sid, hero.id)
    st2 = combat_service.get_combat(db.get(GameSession, sid))
    e1b = combat_service._find(st2, "e1")
    assert e1b["hp"] < e1b["max_hp"]


def _opposed_meta(chunks):
    """从 SSE 分片里取出带 metadata.opposed 的骰事件（对抗卡数据）。"""
    for c in chunks:
        for line in c.splitlines():
            if not line.startswith("data: "):
                continue
            d = json.loads(line[len("data: "):])
            meta = d.get("metadata") or {}
            if d.get("type") == "dice" and "opposed" in meta:
                return meta["opposed"]
    return None


def test_reaction_emits_opposed_card_counter(db_factory, monkeypatch):
    """反击命中 → 骰事件带对抗卡数据：守方（玩家）为胜方、结果『反击得手』。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    state["pending_reaction"] = {
        "attacker_id": "e1", "defender_id": hero.id, "attacker_name": "循声者A",
        "defender_name": hero.name, "weapon": "徒手格斗", "ranged": False,
        "allowed": ["fight_back", "dodge"],
    }
    combat_service._save_combat(db, sid, state)
    _fix_rolls(monkeypatch, [90, 5], die=2)   # e1 失败、hero 成功 → hero 反击得手
    out = asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "fight_back"))
    op = _opposed_meta(out)
    assert op is not None
    assert op["attacker"]["name"] == "循声者A" and op["defender"]["name"] == hero.name
    assert op["winner"] == "defender" and op["result"] == "反击得手"


def test_reaction_emits_opposed_card_dodge(db_factory, monkeypatch):
    """闪避成功 → 对抗卡：守方胜、结果『被闪开/防住』。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    state["pending_reaction"] = {
        "attacker_id": "e1", "defender_id": hero.id, "attacker_name": "循声者A",
        "defender_name": hero.name, "weapon": "徒手格斗", "ranged": False,
        "allowed": ["fight_back", "dodge"],
    }
    combat_service._save_combat(db, sid, state)
    _fix_rolls(monkeypatch, [90, 5], die=2)   # e1 攻击失败、hero 闪避成功 → 守方全身而退
    out = asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "dodge"))
    op = _opposed_meta(out)
    assert op is not None
    assert op["winner"] == "defender" and op["result"] == "被闪开/防住"


def test_reaction_opposed_card_attacker_whiff_is_未命中(db_factory, monkeypatch):
    """攻方自己 roll 失手、守方闪避也没成→结果应是『未命中』且无胜方（不能算守方防住）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    state["pending_reaction"] = {
        "attacker_id": "e1", "defender_id": hero.id, "attacker_name": "循声者A",
        "defender_name": hero.name, "weapon": "徒手格斗", "ranged": False,
        "allowed": ["fight_back", "dodge"],
    }
    combat_service._save_combat(db, sid, state)
    # e1 格斗30：roll 90 失败；hero 闪避35：roll 80 也失败 → 双失手
    _fix_rolls(monkeypatch, [90, 80], die=2)
    out = asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "dodge"))
    op = _opposed_meta(out)
    assert op is not None
    assert op["winner"] is None and op["result"] == "未命中"


def test_armor_reduces_physical_damage(db_factory):
    """护甲从物理伤害里扣：armor=3 受 5 点 → 净伤 2 入血，并有『护甲挡下 3 点』提示。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "铁皮怪")])
    e1 = combat_service._find(state, "e1"); e1["armor"] = 3
    hp0 = e1["hp"]
    lines = combat_service.apply_damage(db, state, e1, 5, reason="测试")
    assert e1["hp"] == hp0 - 2
    assert any("护甲挡下 3 点" in ln for ln in lines)


def test_armor_fully_absorbs(db_factory):
    """护甲≥伤害 → 完全挡下，HP 不变。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "重甲怪")])
    e1 = combat_service._find(state, "e1"); e1["armor"] = 10
    hp0 = e1["hp"]
    lines = combat_service.apply_damage(db, state, e1, 5, reason="")
    assert e1["hp"] == hp0
    assert any("护甲挡下 5 点" in ln for ln in lines)


def test_burning_ignores_armor(db_factory):
    """持续燃烧等能量伤害无视护甲（ignore_armor=True）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "披甲者")])
    e1 = combat_service._find(state, "e1"); e1["armor"] = 10
    hp0 = e1["hp"]
    combat_service.apply_damage(db, state, e1, 4, reason="持续燃烧", ignore_armor=True)
    assert e1["hp"] == hp0 - 4


def test_participant_reads_armor_from_sheet_and_npc(db_factory):
    """参战方护甲值从角色卡 system_data.armor / NPC dict.armor 读入。"""
    db = db_factory(); sid, hero = _seed(db)
    hero.system_data = dict(hero.system_data or {}) | {"armor": 2}
    p = combat_service._char_participant(hero, "player")
    assert p["armor"] == 2
    npc = combat_service._npc_participant({"id": "e", "name": "甲兵", "armor": 5})
    assert npc["armor"] == 5
    assert combat_service._npc_participant({"id": "e2", "name": "裸奔"})["armor"] == 0


def test_impale_damage_flags_penetration():
    """贯穿武器大成功/极难加伤时，flags 带『贯穿』供前端标注；非贯穿武器不带。"""
    from app.rules.coc import combat as coc
    dmg = coc.roll_weapon_damage({"name": "步枪", "dam": "2D6+4", "tho": 1}, "0", impale=True)
    assert "贯穿" in dmg["flags"]
    plain = coc.roll_weapon_damage({"name": "棍", "dam": "1D6", "tho": 0}, "0", impale=True)
    assert "贯穿" not in plain["flags"]


def test_burst_capacity_parses_round():
    """连发射速上限 = 武器 round 括号内数字；单发/慢速装填 → 1（不可连发）。"""
    from app.rules.coc import combat as coc
    assert coc.burst_capacity({"round": "1(3)"}) == 3
    assert coc.burst_capacity({"round": "1"}) == 1
    assert coc.burst_capacity({"round": "1/4"}) == 1


def test_burst_switch_target_accumulates_penalty(db_factory, monkeypatch):
    """连发换目标每换一个 +1 惩罚骰：打 [e1,e1,e2] → 命中检定 penalty 序列 [0,0,1]。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲"), _mk_enemy("e2", "乙")])
    hp = combat_service._find(state, hero.id); hp["skills"] = {"射击(手枪)": 60}
    combat_service._save_combat(db, sid, state)
    seen_pen: list = []
    orig = combat_service.engine.resolve_attack

    def spy(*a, **k):
        seen_pen.append(k.get("penalty"))
        return orig(*a, **k)
    monkeypatch.setattr(combat_service.engine, "resolve_attack", spy)
    _fix_rolls(monkeypatch, [50] * 20, die=2)
    action = {"type": "attack", "weapon": ".38(9mm)左轮", "shots": ["e1", "e1", "e2"]}
    asyncio.run(combat_service.resolve_player_action(db, sid, hero.id, action))
    assert seen_pen == [0, 0, 1]


def test_burst_emits_shots_event(db_factory, monkeypatch):
    """连发落一条带 metadata.burst 的事件，每发一条 shot（含命中/惩罚骰）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    hp = combat_service._find(state, hero.id); hp["skills"] = {"射击(手枪)": 60}
    combat_service._save_combat(db, sid, state)
    _fix_rolls(monkeypatch, [50] * 20, die=2)
    action = {"type": "attack", "weapon": ".38(9mm)左轮", "shots": ["e1", "e1", "e1"]}
    out = asyncio.run(combat_service.resolve_player_action(db, sid, hero.id, action))
    burst = None
    for c in out:
        for line in c.splitlines():
            if line.startswith("data: "):
                d = json.loads(line[len("data: "):])
                if (d.get("metadata") or {}).get("combat_burst"):
                    burst = d["metadata"]
    assert burst is not None and len(burst["shots"]) == 3
    assert all(s["penalty"] == 0 for s in burst["shots"])   # 同目标连开不加罚


def test_burst_single_shot_downgrades_to_normal(db_factory, monkeypatch):
    """shots 不足 2 发 → 降级为单发两段式攻击（挂 pending_roll 等玩家掷伤害），不进连射。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    hp = combat_service._find(state, hero.id); hp["skills"] = {"射击(手枪)": 60}
    combat_service._save_combat(db, sid, state)
    _fix_rolls(monkeypatch, [50] * 8, die=2)
    action = {"type": "attack", "weapon": ".38(9mm)左轮", "shots": ["e1"]}
    asyncio.run(combat_service.resolve_player_action(db, sid, hero.id, action))
    st = combat_service.get_combat(db.get(GameSession, sid))
    assert st.get("pending_roll") and st["pending_roll"]["kind"] == "damage"   # 走了单发两段式


def test_resolve_move_updates_pos_and_budget(db_factory):
    """移动：更新坐标、按 Chebyshev 距离扣 move_left、不推进先攻（同回合仍可攻击）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    hp = combat_service._find(state, hero.id)
    start = dict(hp["pos"]); ml = hp["move_left"]
    dest = {"x": start["x"] - 1, "y": start["y"]}       # 往左一格（空格、远离敌人）
    combat_service.resolve_move(db, sid, hero.id, dest)
    st = combat_service.get_combat(db.get(GameSession, sid))
    h = combat_service._find(st, hero.id)
    assert h["pos"] == dest
    assert h["move_left"] == ml - 1
    assert combat_service.current_actor(st)["id"] == hero.id   # 未推进先攻


def test_resolve_move_rejects_occupied_and_over_budget(db_factory):
    """移动拒绝：落在他人占用格 / 超出移动预算。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    e1 = combat_service._find(state, "e1")
    with pytest.raises(ValueError):
        combat_service.resolve_move(db, sid, hero.id, dict(e1["pos"]))   # 敌人占用
    hp = combat_service._find(state, hero.id)
    hp["move_left"] = 1
    combat_service._save_combat(db, sid, state)
    far = {"x": hp["pos"]["x"], "y": (hp["pos"]["y"] + 3) % 8}
    with pytest.raises(ValueError):
        combat_service.resolve_move(db, sid, hero.id, far)               # 3 格 > 预算 1


def test_player_melee_requires_adjacent(db_factory):
    """近战攻击不相邻 → 拒绝并提示先移动接近（不结算、不推进）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    combat_service._find(state, hero.id)["pos"] = {"x": 0, "y": 0}
    combat_service._find(state, "e1")["pos"] = {"x": 10, "y": 7}
    combat_service._save_combat(db, sid, state)
    with pytest.raises(ValueError, match="移动接近"):
        asyncio.run(combat_service.resolve_player_action(
            db, sid, hero.id, {"type": "attack", "target_id": "e1", "weapon": "徒手格斗"}))


def test_start_combat_lays_grid_and_positions(db_factory):
    """开战落方格：state.grid 存在，参战方都有 pos，敌我隔开（拉开布阵）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    assert state["grid"]["cols"] == 12 and state["grid"]["rows"] == 8
    hp = combat_service._find(state, hero.id); e1 = combat_service._find(state, "e1")
    assert hp["pos"] and e1["pos"]
    from app.rules.coc import positioning
    assert not positioning.is_adjacent(hp, e1)   # 拉开布阵，开局不相邻


def test_reaction_flank_penalty_applied(db_factory, monkeypatch):
    """被 2 名相邻敌人夹击时，反应（闪避）检定吃 -1 夹击惩罚骰。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲"), _mk_enemy("e2", "乙")])
    _cluster_adjacent(db, sid, state)   # hero 与两敌相邻，构成夹击
    state["pending_reaction"] = {"attacker_id": "e1", "defender_id": hero.id, "attacker_name": "甲",
                                 "defender_name": hero.name, "weapon": "徒手格斗", "ranged": False,
                                 "allowed": ["fight_back", "dodge"]}
    combat_service._save_combat(db, sid, state)
    seen: dict = {}
    orig = combat_service.engine.resolve_attack

    def spy(*a, **k):
        seen["dp"] = k.get("defense_penalty")
        return orig(*a, **k)
    monkeypatch.setattr(combat_service.engine, "resolve_attack", spy)
    _fix_rolls(monkeypatch, [50] * 10, die=2)
    asyncio.run(combat_service.resolve_reaction(db, sid, hero.id, "dodge"))
    assert seen["dp"] == 1   # 默认紧凑布阵下 hero 与两敌都相邻 → 夹击 -1


def test_player_pointblank_bonus_applied(db_factory, monkeypatch):
    """火器抵近射击（≤2 格）→ 命中检定 +1 奖励骰。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    combat_service._find(state, hero.id)["skills"] = {"射击(手枪)": 60}
    _cluster_adjacent(db, sid, state)   # hero 与目标相邻（抵近 ≤2 格）
    seen: dict = {}
    orig = combat_service.engine.resolve_attack

    def spy(*a, **k):
        seen["b"] = k.get("bonus")
        return orig(*a, **k)
    monkeypatch.setattr(combat_service.engine, "resolve_attack", spy)
    _fix_rolls(monkeypatch, [50] * 8, die=2)
    asyncio.run(combat_service.resolve_player_action(
        db, sid, hero.id, {"type": "attack", "target_id": "e1", "weapon": ".38(9mm)左轮"}))
    assert seen["b"] == 1   # 抵近相邻 +1（无瞄准）


def test_player_firearm_blocked_los_rejected(db_factory):
    """射击视线被墙挡断 → 拒绝并提示，不结算。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    hp = combat_service._find(state, hero.id); e1 = combat_service._find(state, "e1")
    hp["skills"] = {"射击(手枪)": 60}
    hp["pos"] = {"x": 0, "y": 0}; e1["pos"] = {"x": 4, "y": 0}
    state["grid"]["blocked"] = ["2,0"]
    combat_service._save_combat(db, sid, state)
    with pytest.raises(ValueError, match="视线"):
        asyncio.run(combat_service.resolve_player_action(
            db, sid, hero.id, {"type": "attack", "target_id": "e1", "weapon": ".38(9mm)左轮"}))


def test_npc_walks_toward_unreachable_target(db_factory):
    """NPC 够不着真人（拉开距离）时先走位接近，不落 pending_reaction。"""
    db = db_factory(); sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid, [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    combat_service._find(state, hero.id)["pos"] = {"x": 1, "y": 4}
    combat_service._find(state, "npc_thug")["pos"] = {"x": 10, "y": 4}   # 拉开，够不着近战
    combat_service._save_combat(db, sid, state)
    asyncio.run(combat_service.drive_npcs(db, sid, state))
    st = combat_service.get_combat(db.get(GameSession, sid))
    thug = combat_service._find(st, "npc_thug")
    assert thug["pos"]["x"] < 10                # 朝 hero（左）移动了
    assert st.get("pending_reaction") is None   # 是走位、不是攻击暂停


def test_dash_uses_full_mov_and_ends_turn(db_factory):
    """冲刺：用满 mov 移动（常规预算 ⌈mov/2⌉ 够不到的距离），且独占本回合（推进先攻、清空 move_left）。"""
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "甲")])
    combat_service._find(state, hero.id)["pos"] = {"x": 1, "y": 4}
    combat_service._save_combat(db, sid, state)
    dest = {"x": 8, "y": 4}   # 距 (1,4) 7 格：mov 8 冲刺可达，常规预算 4 够不到
    with pytest.raises(ValueError):
        combat_service.resolve_move(db, sid, hero.id, dest, dash=False)
    combat_service.resolve_move(db, sid, hero.id, dest, dash=True)
    st = combat_service.get_combat(db.get(GameSession, sid))
    h = combat_service._find(st, hero.id)
    assert h["pos"] == dest and h["move_left"] == 0
    assert st["turn_index"] != 0   # 冲刺独占回合 → 已推进先攻


def test_extinguish_action_removes_burning(db_factory):
    db = db_factory(); sid, hero = _seed(db)
    state = _start_multi(db, sid, hero, [_mk_enemy("e1", "循声者A")])
    hero_p = combat_service._find(state, hero.id)
    hero_p["conditions"] = ["burning"]
    combat_service._apply_one_action(db, sid, state, hero_p, {"type": "extinguish"})
    assert "burning" not in hero_p.get("conditions", [])


def test_combat_meta_order_carries_conditions_aim_weapon(db_factory):
    """_combat_meta 的 order 投影要透传 conditions/aim/weapon，供前端 HUD 渲染徽标与武器名。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 30}, "weapon": "大棒(棒球棒、拨火棍)"}
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
    assert by_id["npc_thug"]["weapon"] == "大棒(棒球棒、拨火棍)"   # 武器名随 order 透传，卡片才能显示
    assert by_id[hero.id]["aim"] is True
    assert by_id[hero.id]["conditions"] == []
    assert by_id[hero.id]["weapon"]   # 玩家武器非空（默认徒手格斗）


def test_combat_meta_carries_started_seq_boundary(db_factory):
    """start_combat 记本场起点 seq，_combat_meta 透传——重连让日志抽屉只收本场（seq>started_seq）。

    同一会话第 2 场战斗的 started_seq 必须 > 第 1 场，否则重连时抽屉会掺上一场结算行。
    """
    db = db_factory()
    sid, hero = _seed(db)
    hero_p = [combat_service._char_participant(hero, "player", is_human=True)]

    state1 = combat_service.start_combat(db, sid, hero_p, [])
    seq1 = state1["started_seq"]
    assert isinstance(seq1, int)
    assert combat_service._combat_meta(state1)["started_seq"] == seq1

    # 第 1 场落几条机械结算行（推高会话 seq），结束后再起第 2 场。
    combat_service._combat_line(db, sid, "打手 受到 3 点伤害（HP 8→5）")
    combat_service._combat_line(db, sid, "打手 倒地不起")
    combat_service._save_combat(db, sid, None)   # combat_end：pop 战斗态

    state2 = combat_service.start_combat(db, sid, hero_p, [])
    seq2 = state2["started_seq"]
    assert seq2 > seq1   # 第 2 场起点在第 1 场结算行之后 → 抽屉过滤掉第 1 场
    assert combat_service._combat_meta(state2)["started_seq"] == seq2


def test_start_pauses_at_human_and_broadcasts_state(db_factory):
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45}, "weapon": "徒手格斗"}
    state, chunks = _start(db, sid, hero, enemy, trigger="打手扑上来")
    actor = combat_service.current_actor(state)
    assert actor["id"] == hero.id and actor["is_human"] is True    # DEX70>40，先攻首位是英雄
    assert any('"combat_start"' in c for c in chunks)


def test_player_attack_two_step_hit_then_damage(db_factory, monkeypatch):
    """两段式：真人攻击命中后先挂 pending_roll（不扣血），玩家亲自掷伤害才扣血、推进。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state, _ = _start(db, sid, hero, enemy)
    _cluster_adjacent(db, sid, state)          # 近战须相邻
    _fix_rolls(monkeypatch, [10, 80], die=3)   # 英雄攻中(10)/打手闪避失败(80) → 命中
    chunks = _act(db, sid, hero.id, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})

    # 第一段：命中检定作为 3D 骰事件（带 metadata.dice）下发，挂 pending_roll，尚未扣血。
    st = combat_service.get_combat(db.get(GameSession, sid))
    assert st["pending_roll"] and st["pending_roll"]["actor_id"] == hero.id
    thug = next(p for p in st["initiative"] if p["id"] == "npc_thug")
    assert thug["hp"] == thug["max_hp"]                       # 伤害还没结算
    assert any('"combat_roll"' in c and '"dice"' in c for c in chunks)   # 命中骰事件（可动画）

    # 未完成投掷前不许再提交行动。
    with pytest.raises(ValueError, match="待掷"):
        _act(db, sid, hero.id, {"type": "dodge"})

    # 第二段：玩家亲自掷伤害 → 扣血、清 pending_roll、推进先攻。
    roll_chunks = _roll(db, sid, hero.id)
    st2 = combat_service.get_combat(db.get(GameSession, sid))
    thug2 = next(p for p in st2["initiative"] if p["id"] == "npc_thug")
    assert thug2["hp"] < thug2["max_hp"]                      # 伤害已结算
    assert not st2.get("pending_roll")
    assert any('"combat_roll"' in c and '"dice"' in c for c in roll_chunks)


def test_player_attack_miss_no_pending_roll(db_factory, monkeypatch):
    """未命中：不挂 pending_roll，直接推进（无第二段伤害投掷）。"""
    db = db_factory()
    sid, hero = _seed(db)
    enemy = {"id": "npc_thug", "name": "打手", "attributes": {"DEX": 40, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state, _ = _start(db, sid, hero, enemy)
    _cluster_adjacent(db, sid, state)          # 近战须相邻
    _fix_rolls(monkeypatch, [99, 10], die=3)   # 英雄大失败(99) → 未命中
    chunks = _act(db, sid, hero.id, {"type": "attack", "target_id": "npc_thug", "weapon": "徒手格斗"})
    st = combat_service.get_combat(db.get(GameSession, sid))
    assert st is None or not st.get("pending_roll")   # 未命中不挂待掷
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
    state, _ = _start(db, sid, hero, enemy)
    _cluster_adjacent(db, sid, state)         # 近战须相邻
    _fix_rolls(monkeypatch, [1, 90], die=8)   # 英雄大成功命中、伤害拉满，秒掉 HP=2 的敌人
    _act(db, sid, hero.id, {"type": "attack", "target_id": "npc_weak", "weapon": "大棒(棒球棒、拨火棍)"})
    # 命中挂 pending_roll，战斗尚未结束（伤害待玩家掷）
    assert combat_service.get_combat(db.get(GameSession, sid))["pending_roll"]
    chunks = _roll(db, sid, hero.id)   # 玩家掷伤害 → 秒掉敌人 → 战斗结束

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
        db, sid, [hero], [boss], {hero.id}, agent=agent,
        deployment={hero.id: {"x": 5, "y": 5}, "npc_boss": {"x": 6, "y": 5}}))   # 开局相邻
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
    _cluster_adjacent(db, sid, state)   # 打手开局就够得着 hero → 直接攻击暂停
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
    _cluster_adjacent(db, sid, state)   # NPC 开局就够得着 hero → 攻击暂停
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
    _cluster_adjacent(db, sid, state)   # NPC 开局就够得着（被擒抱者也相邻）
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
