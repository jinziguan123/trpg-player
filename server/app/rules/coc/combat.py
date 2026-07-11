"""CoC 7th 战斗轮的**纯确定性**规则引擎（P1 地基，不接 LLM、不碰 DB/广播）。

只负责规则计算：先攻排序（火器优先）、武器伤害、攻击解析（近战对抗 / 远程检定 / 命中伤害 /
贯穿）、启发式 NPC 选择、回合推进、结束判定。状态机 / 上下文 / 落库 / 前端在 P2+ 另接。

复用现有原语：resolve_skill_check（达成等级/成败）、roll（骰池）、COC_WEAPONS（武器表）。
掷骰经 roll / roll_percentile，测试可 monkeypatch 钉死。
"""

from __future__ import annotations

import math
import random
import re

from app.rules.coc.checks import resolve_skill_check
from app.rules.coc.weapons import COC_WEAPONS

# 成功等级排序（与 chat_service._OUTCOME_RANK 一致，保证战斗对抗与既有 opposed_check 同语义）
OUTCOME_RANK = {
    "critical_success": 4,
    "hard_success": 3,
    "success": 2,
    "failure": 1,
    "fumble": 0,
}

WEAPON_BY_NAME: dict[str, dict] = {w["name"]: w for w in COC_WEAPONS}

_UNARMED = {"name": "徒手格斗", "skill": "格斗(斗殴)", "dam": "1D3+DB", "tho": 0}

# 近战/远程战斗技能前缀，用于「平手比战斗技能」与 fight_back 默认技能
_FIGHT_PREFIXES = ("格斗", "斗殴")
_SHOOT_PREFIXES = ("射击", "枪")


def resolve_weapon(name: str | None) -> dict:
    """按名字取武器数据：精确 → 子串 → 回落徒手。名字可为 KP 给的通俗名（如「匕首」）。"""
    if not name:
        return _UNARMED
    n = name.strip()
    if n in WEAPON_BY_NAME:
        return WEAPON_BY_NAME[n]
    for w in COC_WEAPONS:
        if n in w["name"] or w["name"] in n:
            return w
    return _UNARMED


def _roll_expr(expr: str) -> tuple[int, list[int]]:
    """掷一个多项骰式（如 '1D8+1D6+3'、'1D8-2'、'2D6'）：返回 (总和, 各骰点)。
    非数字项（燃烧/晕/码 等注记）忽略。用于武器伤害。"""
    expr = (expr or "").replace(" ", "").replace("－", "-")
    total, rolls = 0, []
    for term in re.findall(r"[+-]?[^+-]+", expr):
        sign = -1 if term.startswith("-") else 1
        t = term.lstrip("+-")
        m = re.fullmatch(r"(\d+)[dD](\d+)", t)
        if m:
            c, s = int(m.group(1)), int(m.group(2))
            r = [random.randint(1, s) for _ in range(c)]
            rolls += r
            total += sign * sum(r)
        elif t.isdigit():
            total += sign * int(t)
        # 其它（DB 已在上层替换掉；剩下的燃烧/晕/码等注记）忽略
    return total, rolls


def _max_dice(expr: str) -> int:
    """骰式的最大点数（贯穿/大成功用）：每个 NdM 记 N*M，flat 记其值，负项相减。"""
    expr = (expr or "").replace(" ", "").replace("－", "-")
    total = 0
    for term in re.findall(r"[+-]?[^+-]+", expr):
        sign = -1 if term.startswith("-") else 1
        t = term.lstrip("+-")
        m = re.fullmatch(r"(\d+)[dD](\d+)", t)
        if m:
            total += sign * int(m.group(1)) * int(m.group(2))
        elif t.isdigit():
            total += sign * int(t)
    return total


def _substitute_db(dam: str, db: str) -> str:
    """把武器伤害公式里的 DB / 半DB 替换成具体角色的伤害加值表达式。
    半DB：掷出 DB 取半（floor），作为定值代入（贯穿/远程小武器用）。"""
    db = (db or "0").strip()
    expr = dam
    if "半DB" in expr:
        half = math.floor(_roll_expr(db)[0] / 2)
        expr = expr.replace("半DB", f"{'+' if half >= 0 else ''}{half}")
    if "DB" in expr:
        expr = expr.replace("DB", f"{'+' if not db.startswith('-') else ''}{db}")
    return expr


def roll_weapon_damage(weapon: str | dict, db: str = "0", *, impale: bool = False) -> dict:
    """掷武器伤害。weapon 可为武器名或武器 dict。impale=贯穿/大成功（贯穿武器）→ 最大骰点 + 再掷一次。

    返回 {total, rolls, notation, flags}。flags 收 燃烧/晕/贯穿 等注记（供叙述），不计入数值。
    霰弹/射程分段公式（'2D6+2/1D6+1/1D4'）取首段（近距离）。伤害不为负。
    """
    w = resolve_weapon(weapon) if isinstance(weapon, str) else weapon
    dam = (w.get("dam") or "1D3").split("/")[0].strip()   # 取首段（近距离）
    flags = [kw for kw in ("燃烧", "烧", "晕") if kw in dam]
    expr = _substitute_db(dam, db)
    total, rolls = _roll_expr(expr)
    if impale and w.get("tho"):
        # 贯穿：加上武器骰的最大点数（DB 不翻）——RAW「max + 掷一次」的可测版
        weapon_only = re.sub(r"([+-])(?![0-9]*[dD])[0-9]+", "", dam)  # 去掉纯定值项，只留骰
        total += _max_dice(_substitute_db(weapon_only, "0"))
    return {"total": max(0, total), "rolls": rolls, "notation": dam, "flags": flags}


def combat_skill_value(p: dict) -> int:
    """参战方最高的战斗技能值（格斗/射击），用于先攻平手比较。"""
    skills = p.get("skills") or {}
    vals = [v for k, v in skills.items()
            if any(k.startswith(pre) for pre in _FIGHT_PREFIXES + _SHOOT_PREFIXES)]
    return max(vals) if vals else 0


_SIDE_RANK = {"player": 0, "ally": 1, "enemy": 2}


def roll_initiative(participants: list[dict]) -> list[dict]:
    """按 (火器优先, DEX 降序, 战斗技能降序, 玩家先于 NPC, id 稳定) 排先攻序。

    participants 每项需含：id, dex, has_firearm(bool), side, skills。返回排好序的新列表。
    """
    def key(p: dict):
        return (
            0 if p.get("has_firearm") else 1,           # 火器组整体在前
            -int(p.get("dex") or 0),                    # DEX 降序
            -combat_skill_value(p),                     # 平手比战斗技能
            _SIDE_RANK.get(p.get("side"), 3),           # 仍平：玩家 < 队友 < 敌
            str(p.get("id") or ""),                     # 再平：稳定
        )
    return sorted(participants, key=key)


def compare_checks(a, b) -> str:
    """比两次检定的成功等级：返回 'a' / 'b' / 'tie'。同级比技能值高者胜，再平则平。"""
    ar, br = OUTCOME_RANK.get(a.outcome, 1), OUTCOME_RANK.get(b.outcome, 1)
    if ar != br:
        return "a" if ar > br else "b"
    if a.skill_value != b.skill_value:
        return "a" if a.skill_value > b.skill_value else "b"
    return "tie"


def _fight_skill_of(data: dict) -> str:
    """角色的近战技能名（用于反击默认）：优先已有的格斗(X)，否则回落 格斗(斗殴)。"""
    for k in (data.get("skills") or {}):
        if any(k.startswith(pre) for pre in _FIGHT_PREFIXES):
            return k
    return "格斗(斗殴)"


def allowed_reactions(is_firearm: bool, defender_grappled: bool = False) -> list[str]:
    """被攻击者可选的反应。火器不能反击（RAW），只能闪避/扑掩体。

    被擒抱（defender_grappled）者无法闪避/扑掩体：近战只剩反击，火器则无从躲避（空列表，命中即结算）。
    defender_grappled 默认 False，保持向后兼容。
    """
    if defender_grappled:
        return [] if is_firearm else ["fight_back"]
    return ["dodge", "cover"] if is_firearm else ["fight_back", "dodge"]


def resolve_first_aid(medic_data: dict, skill: str = "急救") -> dict:
    """一次急救/医学检定。返回 {success, heal, lines}：成功 heal=1（回 1 HP），失败 heal=0。

    纯规则：只算成败与回血量。是否稳住濒死、写回角色卡、标记该处伤已急救，由 service 层决定。
    """
    chk = resolve_skill_check(medic_data, skill)
    success = chk.outcome in ("critical_success", "hard_success", "success")
    heal = 1 if success else 0
    verb = "成功" if success else "失败"
    lines = [f"{skill}检定{verb}（{chk.description}）" + ("，稳住伤势 +1 HP。" if success else "，未能处理伤势。")]
    return {"success": success, "heal": heal, "lines": lines}


def resolve_observe(observer_data: dict, skill: str = "侦查") -> dict:
    """一次观察战场检定（侦查/心理学）。返回 {success, lines}。

    成功不改数值，仅产一条「察觉到破绽/敌情」beat，交子代理据此叙述具体线索。
    """
    chk = resolve_skill_check(observer_data, skill)
    success = chk.outcome in ("critical_success", "hard_success", "success")
    if success:
        lines = [f"{skill}检定成功（{chk.description}），观察到敌方的破绽/动向。"]
    else:
        lines = [f"{skill}检定失败（{chk.description}），未能看出更多。"]
    return {"success": success, "lines": lines}


def resolve_maneuver(attacker_data: dict, defender_data: dict, kind: str = "grapple") -> dict:
    """对抗格斗机动（擒抱/缴械）。attacker 的格斗 对抗 defender 的格斗/闪避（取其一），
    用 compare_checks 比成功等级。返回 {success, condition, lines}：

    attacker 严格胜出 → success=True、condition = 'grappled'（擒抱）或 'disarmed'（缴械）；
    平手/败 → success=False、condition=None。不造成伤害。
    """
    atk_skill = _fight_skill_of(attacker_data)
    # 守方用更擅长的一项抵抗（格斗 or 闪避）
    dfn_skill = _fight_skill_of(defender_data)
    dfn_skills = defender_data.get("skills") or {}
    if (dfn_skills.get("闪避") or 0) > (dfn_skills.get(dfn_skill) or 0):
        dfn_skill = "闪避"
    atk = resolve_skill_check(attacker_data, atk_skill)
    dfn = resolve_skill_check(defender_data, dfn_skill)
    won = compare_checks(atk, dfn) == "a" and atk.meets_difficulty
    condition = None
    kind_cn = "擒抱" if kind == "grapple" else "缴械"
    if won:
        condition = "grappled" if kind == "grapple" else "disarmed"
        lines = [f"{kind_cn}对抗（{atk.description} vs {dfn.description}）→ 得手。"]
    else:
        lines = [f"{kind_cn}对抗（{atk.description} vs {dfn.description}）→ 未能得手。"]
    return {"success": won, "condition": condition, "lines": lines}


def resolve_attack(
    attacker_data: dict,
    attacker_db: str,
    weapon: str | dict,
    *,
    defender_data: dict | None = None,
    defense: str | None = None,     # None(无反应) | 'dodge' | 'cover' | 'fight_back'
    ranged: bool = False,
    difficulty: str = "normal",
    attacker_disarmed: bool = False,
    bonus: int = 0,
) -> dict:
    """解析一次攻击。返回结构化结果（谁命中谁、伤害多少、各自检定）。**不改状态、不落库。**

    - 近战 + defense='dodge'/'cover'：攻方武器技能 对抗 守方闪避；攻方成功等级更高才命中（平/低=被闪开）。
    - 近战 + defense='fight_back'：双方各掷格斗，胜方伤害负方（攻→守 或 守→攻反击）。
    - 远程 + defense=None：攻方射击检定，达到要求难度即命中。
    - 远程 + defense='dodge'/'cover'：射手转困难难度（扑向掩体）重掷，命中即伤、无反击。
    命中且为贯穿武器 + 极难/大成功 → 贯穿加伤。

    attacker_disarmed=True：攻方已被缴械 → 武器强制回落徒手格斗。
    bonus>0：给攻方命中检定加奖励骰（瞄准用，透传给 resolve_skill_check）。
    """
    if attacker_disarmed:
        weapon = _UNARMED
    w = resolve_weapon(weapon) if isinstance(weapon, str) else weapon
    atk_skill = w.get("skill") or "格斗(斗殴)"
    atk = resolve_skill_check(attacker_data, atk_skill, difficulty, bonus=bonus)

    result: dict = {
        "weapon": w.get("name"), "attacker_check": atk, "defender_check": None,
        "hit": False, "damage": None, "damage_to": None, "defense": defense,
    }

    def _damage(tier: str) -> dict:
        impale = bool(w.get("tho")) and tier in ("extreme", "critical")
        return roll_weapon_damage(w, attacker_db, impale=impale)

    # 远程：无反应 → 达标即命中；扑掩体/闪避 → 射手转困难难度（扑向掩体），无反击
    if ranged:
        if defense in ("dodge", "cover"):
            atk = resolve_skill_check(attacker_data, atk_skill, "hard", bonus=bonus)
            result["attacker_check"] = atk
        if atk.meets_difficulty:
            result["hit"] = True
            result["damage"] = _damage(atk.tier)
            result["damage_to"] = "defender"
        return result
    if defense is None:
        if atk.meets_difficulty:
            result["hit"] = True
            result["damage"] = _damage(atk.tier)
            result["damage_to"] = "defender"
        return result

    # 近战：需要守方数据
    if defender_data is None:
        # 无守方信息 → 退化为「攻方达标即命中」
        if atk.meets_difficulty:
            result["hit"] = True
            result["damage"] = _damage(atk.tier)
            result["damage_to"] = "defender"
        return result

    if defense in ("dodge", "cover"):
        dfn = resolve_skill_check(defender_data, "闪避", "normal")
        result["defender_check"] = dfn
        # 攻方成功等级严格高于守方才命中（平手/守方更高 = 被闪开）
        if compare_checks(atk, dfn) == "a" and atk.meets_difficulty:
            result["hit"] = True
            result["damage"] = _damage(atk.tier)
            result["damage_to"] = "defender"
        return result

    # fight_back：双方格斗，胜方伤害负方
    dfn_skill = _fight_skill_of(defender_data)
    dfn = resolve_skill_check(defender_data, dfn_skill, "normal")
    result["defender_check"] = dfn
    winner = compare_checks(atk, dfn)
    if winner == "a" and atk.meets_difficulty:
        result["hit"] = True
        result["damage"] = _damage(atk.tier)
        result["damage_to"] = "defender"
    elif winner == "b" and dfn.meets_difficulty:
        # 守方反击命中攻方（用守方的徒手/格斗武器；此处按徒手估伤，具体武器由上层传入更佳）
        result["hit"] = True
        result["damage"] = roll_weapon_damage(_UNARMED, defender_data.get("_db", "0"),
                                               impale=dfn.tier in ("extreme", "critical") and False)
        result["damage_to"] = "attacker"
    return result


def resolve_wound(hp: int, max_hp: int, damage: int, defender_data: dict) -> dict:
    """结算一次伤害的 HP 与状态迁移（纯规则，不碰 DB）。
    返回 {new_hp, status, lines}。重伤（单击≥半血）触发 CON 检定，失败则昏迷。"""
    max_hp = max(1, max_hp)   # 归一非正 max_hp，避免半血阈值/贯穿判定被负值扭曲
    raw = hp - damage
    new_hp = max(0, raw)
    lines = [f"受到 {damage} 点伤害（HP {hp}→{new_hp}）"]
    major = damage >= max_hp // 2
    if raw <= -max_hp:
        return {"new_hp": new_hp, "status": "dead", "lines": lines + ["当场毙命！"]}
    if new_hp <= 0:
        return {"new_hp": 0, "status": "dying", "lines": lines + ["濒死，需急救/医学稳定。"]}
    if major:
        con = resolve_skill_check(defender_data, "体质", "normal")
        lines.append(f"重伤体质检定：{con.description}")
        if con.outcome in ("failure", "fumble"):
            return {"new_hp": new_hp, "status": "unconscious", "lines": lines + ["眼前一黑，昏迷倒地！"]}
        return {"new_hp": new_hp, "status": "major_wound", "lines": lines}
    return {"new_hp": new_hp, "status": "ok", "lines": lines}


def tick_dying(participant: dict) -> list[str]:
    """濒死者每轮开始的 CON 检定：失败则死亡。非濒死者 no-op。原地改 participant。"""
    if participant.get("status") != "dying":
        return []
    data = {"skills": participant.get("skills") or {},
            "base_attributes": participant.get("base_attributes") or {},
            "system_data": participant.get("system_data") or {}}
    con = resolve_skill_check(data, "体质", "normal")
    if con.outcome in ("failure", "fumble"):
        participant["status"] = "dead"
        return [f"{participant.get('name', '')}｜濒死体质检定失败（{con.description}），气绝身亡。"]
    return [f"{participant.get('name', '')}｜濒死体质检定{con.description}，暂时挺住。"]


# ── 参战方存活 / 结束判定 / 回合推进 / 启发式 ──────────────────────────

_DOWN_STATUS = {"dead", "dying", "fled"}


def is_active(p: dict) -> bool:
    """仍能行动：状态非 死亡/濒死/逃离，且 HP>0。"""
    return p.get("status") not in _DOWN_STATUS and (p.get("hp") or 0) > 0


def check_combat_end(participants: list[dict]) -> str | None:
    """一方无人能战即结束。返回 'players_win' / 'players_defeated' / 'no_combatants' / None。

    玩家方 = player + ally；敌方 = enemy。
    """
    players = [p for p in participants if p.get("side") in ("player", "ally")]
    enemies = [p for p in participants if p.get("side") == "enemy"]
    p_alive = any(is_active(p) for p in players)
    e_alive = any(is_active(p) for p in enemies)
    if not p_alive and not e_alive:
        return "no_combatants"
    if not e_alive:
        return "players_win"
    if not p_alive:
        return "players_defeated"
    return None


def advance_turn(state: dict) -> dict:
    """推进到下一个「仍能行动」的参战方。走完一圈 → round++、重排先攻、清本轮标记。原地改 state。"""
    order = state.get("initiative") or []
    n = len(order)
    if n == 0:
        return state
    for _ in range(n):
        state["turn_index"] = (state.get("turn_index", 0) + 1) % n
        if state["turn_index"] == 0:
            state["round"] = state.get("round", 1) + 1
            for p in order:
                p["acted_this_round"] = False
                p["dodged_this_round"] = False
            state["initiative"] = roll_initiative(order)
            order = state["initiative"]
        if is_active(order[state["turn_index"]]):
            return state
    return state  # 无人可动（由 check_combat_end 收尾）


def heuristic_defense(defender: dict, is_firearm: bool, defender_grappled: bool = False) -> str:
    """NPC 防御者的自动防御选择（真人防御走交互，不经此）。
    火器只能闪避；近战：好斗者反击，其余闪避。
    被擒抱（defender_grappled）且近战 → 无法闪避，只能反击。defender_grappled 默认 False 向后兼容。"""
    if is_firearm:
        return "dodge"
    if defender_grappled:
        return "fight_back"
    return "fight_back" if defender.get("combat_ai") == "aggressive" else "dodge"


def heuristic_npc_action(state: dict, actor: dict) -> dict:
    """杂兵启发式：攻击对方阵营中 HP 最低的存活者；HP<25% 且策略为 cautious 则逃跑。
    返回 {action, target_id?, weapon?}。关键 NPC 走子代理（P3），不经此。"""
    hp_ratio = (actor.get("hp") or 0) / max(1, actor.get("max_hp") or 1)
    if actor.get("combat_ai") == "cautious" and hp_ratio < 0.25:
        return {"action": "flee"}
    my_side = actor.get("side")
    foes = [p for p in (state.get("initiative") or [])
            if is_active(p) and (
                (my_side == "enemy" and p.get("side") in ("player", "ally"))
                or (my_side in ("player", "ally") and p.get("side") == "enemy"))]
    if not foes:
        return {"action": "wait"}
    target = min(foes, key=lambda p: (p.get("hp") or 0))
    return {"action": "attack", "target_id": target.get("id"),
            "weapon": actor.get("weapon") or "徒手格斗"}
