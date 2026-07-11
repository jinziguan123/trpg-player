"""CoC 战斗引擎（纯确定性）单测：先攻/伤害/攻击解析/结束判定/回合推进/启发式。"""

from app.rules.coc import combat


def _seq(values):
    """把一串 d100 依次喂给 resolve_skill_check（每次检定取一个）。"""
    it = iter(values)
    return lambda: next(it)


def _fix_randint(monkeypatch, value=3):
    """把武器伤害的骰点钉死为定值（每颗骰=value）。"""
    monkeypatch.setattr(combat.random, "randint", lambda a, b: value)


# ── 先攻（火器优先 + DEX + 技能 + 阵营 + 稳定）──────────────────────────

def test_initiative_firearm_group_first_then_dex():
    parts = [
        {"id": "melee_hi", "dex": 70, "has_firearm": False, "side": "enemy", "skills": {}},
        {"id": "gun_lo", "dex": 50, "has_firearm": True, "side": "enemy", "skills": {}},
        {"id": "melee_lo", "dex": 50, "has_firearm": False, "side": "player", "skills": {}},
    ]
    order = [p["id"] for p in combat.roll_initiative(parts)]
    # 火器组整体在前（哪怕 DEX 更低）；其余按 DEX 降序
    assert order == ["gun_lo", "melee_hi", "melee_lo"]


def test_initiative_tiebreak_by_skill_then_side():
    parts = [
        {"id": "a", "dex": 50, "has_firearm": False, "side": "enemy", "skills": {"格斗(斗殴)": 40}},
        {"id": "b", "dex": 50, "has_firearm": False, "side": "enemy", "skills": {"格斗(斗殴)": 80}},
        {"id": "c", "dex": 50, "has_firearm": False, "side": "player", "skills": {"格斗(斗殴)": 80}},
    ]
    order = [p["id"] for p in combat.roll_initiative(parts)]
    # 同 DEX：技能高者先（b/c=80 先于 a=40）；技能同再比阵营（player c 先于 enemy b）
    assert order == ["c", "b", "a"]


# ── 武器伤害（DB / 半DB / 定值 / 贯穿 / 霰弹分段 / 注记）────────────────

def test_weapon_damage_substitutes_db(monkeypatch):
    _fix_randint(monkeypatch, 3)
    # 大棒 1D8+DB，DB=1d4 → "1D8+1d4" → 3 + 3 = 6
    d = combat.roll_weapon_damage("大棒(棒球棒、拨火棍)", "1d4")
    assert d["total"] == 6 and d["rolls"] == [3, 3]


def test_weapon_damage_half_db(monkeypatch):
    _fix_randint(monkeypatch, 3)
    # 投石 1D4+半DB，DB=1d4：半DB = floor(掷1d4/2)=floor(3/2)=1 → "1D4+1" → 3+1=4
    d = combat.roll_weapon_damage("投石", "1d4")
    assert d["total"] == 4


def test_weapon_damage_flat_and_flags(monkeypatch):
    _fix_randint(monkeypatch, 2)
    d = combat.roll_weapon_damage({"name": "火把", "dam": "1D6+燃烧", "tho": 0}, "1d6")
    assert d["total"] == 2 and "燃烧" in d["flags"]   # 燃烧不计入数值


def test_weapon_damage_impale_adds_max_dice(monkeypatch):
    _fix_randint(monkeypatch, 3)
    w = {"name": "小刀", "dam": "1D4+DB", "tho": 1}
    normal = combat.roll_weapon_damage(w, "0")           # 1D4+0 = 3
    impaled = combat.roll_weapon_damage(w, "0", impale=True)  # + max(1D4)=4 → 7
    assert normal["total"] == 3 and impaled["total"] == 7


def test_weapon_damage_shotgun_takes_first_bracket(monkeypatch):
    _fix_randint(monkeypatch, 2)
    # "2D6+2/1D6+1/1D4" 取首段 2D6+2 → 2+2+2 = 6
    d = combat.roll_weapon_damage({"name": "霰弹枪", "dam": "2D6+2/1D6+1/1D4", "tho": 1}, "0")
    assert d["total"] == 6 and d["notation"] == "2D6+2"


# ── 攻击解析（近战闪避 / 反击 / 远程）──────────────────────────────────

_ATK = {"skills": {"格斗(斗殴)": 60}, "base_attributes": {}}
_DEF = {"skills": {"闪避": 40, "格斗(斗殴)": 50}, "base_attributes": {}}


def test_melee_dodge_hit_when_attacker_outrolls(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([10, 80]))
    _fix_randint(monkeypatch, 3)
    r = combat.resolve_attack(_ATK, "0", "徒手格斗", defender_data=_DEF, defense="dodge")
    assert r["hit"] is True and r["damage_to"] == "defender" and r["damage"]["total"] > 0


def test_melee_dodge_miss_when_defender_dodges(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([80, 30]))
    r = combat.resolve_attack(_ATK, "0", "徒手格斗", defender_data=_DEF, defense="dodge")
    assert r["hit"] is False and r["damage"] is None


def test_fight_back_winner_damages_loser(monkeypatch):
    # 攻方失败、守方成功 → 守方反击命中攻方
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([80, 30]))
    _fix_randint(monkeypatch, 2)
    r = combat.resolve_attack(_ATK, "0", "徒手格斗", defender_data=_DEF, defense="fight_back")
    assert r["hit"] is True and r["damage_to"] == "attacker"


def test_ranged_hit_on_meeting_difficulty(monkeypatch):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq([30]))
    _fix_randint(monkeypatch, 3)
    shooter = {"skills": {"射击(手枪)": 60}, "base_attributes": {}}
    r = combat.resolve_attack(shooter, "0", {"name": "手枪", "dam": "1D8", "skill": "射击(手枪)", "tho": 0},
                              ranged=True)
    assert r["hit"] is True and r["damage"]["total"] == 3


# ── 对抗比较 / 存活 / 结束判定 ────────────────────────────────────────

def test_check_combat_end_states():
    def mk(side, hp, status="ok"):
        return {"side": side, "hp": hp, "status": status}
    assert combat.check_combat_end([mk("player", 10), mk("enemy", 0)]) == "players_win"
    assert combat.check_combat_end([mk("player", 0), mk("enemy", 8)]) == "players_defeated"
    assert combat.check_combat_end([mk("player", 10), mk("enemy", 8)]) is None
    assert combat.check_combat_end([mk("player", 0), mk("enemy", 0)]) == "no_combatants"
    # fled/dying 也算失能
    assert combat.check_combat_end([mk("player", 10), mk("enemy", 5, "fled")]) == "players_win"


# ── 回合推进 ──────────────────────────────────────────────────────────

def test_advance_turn_skips_downed_and_wraps_round():
    order = [
        {"id": "a", "side": "player", "hp": 10, "status": "ok", "dex": 70, "has_firearm": False, "skills": {}},
        {"id": "b", "side": "enemy", "hp": 0, "status": "dead", "dex": 60, "has_firearm": False, "skills": {}},
        {"id": "c", "side": "ally", "hp": 8, "status": "ok", "dex": 50, "has_firearm": False, "skills": {}},
    ]
    state = {"round": 1, "turn_index": 0, "initiative": order}
    combat.advance_turn(state)      # a(0) → 跳过死掉的 b → c(2)
    assert state["turn_index"] == 2 and state["round"] == 1
    combat.advance_turn(state)      # c(2) → 绕回，round++，落到 a(0)
    assert state["turn_index"] == 0 and state["round"] == 2
    assert all(not p["acted_this_round"] for p in state["initiative"])  # 新一轮清标记


# ── 启发式 NPC ────────────────────────────────────────────────────────

def test_heuristic_attacks_lowest_hp_foe():
    state = {"initiative": [
        {"id": "mob", "side": "enemy", "hp": 8, "max_hp": 10, "status": "ok", "weapon": "徒手格斗"},
        {"id": "p1", "side": "player", "hp": 9, "max_hp": 12, "status": "ok"},
        {"id": "p2", "side": "player", "hp": 3, "max_hp": 12, "status": "ok"},
    ]}
    mob = state["initiative"][0]
    act = combat.heuristic_npc_action(state, mob)
    assert act["action"] == "attack" and act["target_id"] == "p2"   # 打 HP 最低的


def test_heuristic_cautious_flees_when_low():
    state = {"initiative": [{"id": "m", "side": "enemy", "hp": 1, "max_hp": 10, "status": "ok"}]}
    act = combat.heuristic_npc_action(state, {"id": "m", "side": "enemy", "hp": 1,
                                              "max_hp": 10, "status": "ok", "combat_ai": "cautious"})
    assert act["action"] == "flee"


# ── P1：反应许可 / 扑掩体 / NPC 防御 / 伤害结算 / 濒死 tick ────────────

def test_allowed_reactions_firearm_excludes_fight_back():
    from app.rules.coc.combat import allowed_reactions
    assert allowed_reactions(is_firearm=False) == ["fight_back", "dodge"]
    assert allowed_reactions(is_firearm=True) == ["dodge", "cover"]
