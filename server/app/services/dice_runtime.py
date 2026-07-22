"""骰子协议、检定对象解析与基础投骰执行。"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import session_service
from app.services.event_protocol import event_to_chunk, make_chunk

_make_chunk = make_chunk

DEFAULT_NPC_SKILL = 45
ALWAYS_BLIND_SKILLS = ("心理学",)
DIFFICULTY_LABEL = {"normal": "", "hard": "困难", "extreme": "极难"}
TIER_LABEL = {
    "critical": "大成功",
    "extreme": "极难成功",
    "hard": "困难成功",
    "regular": "普通成功",
    "fail": "普通失败",
    "fumble": "大失败",
}
_OUTCOME_RANK = {
    "critical_success": 4,
    "hard_success": 3,
    "success": 2,
    "failure": 1,
    "fumble": 0,
}

def _check_prompt_text(actor_name: str, skill: str, difficulty: str) -> str:
    """req 1：系统主动给出的检定提示语。"""
    diff = DIFFICULTY_LABEL.get(difficulty, "")
    if diff:
        return f"请 {actor_name} 进行一次「{diff}」难度的「{skill}」检定"
    return f"请 {actor_name} 进行一次「{skill}」检定"

# 成功等级排序（对抗骰比较用）：大成功 > 困难/极难成功 > 普通成功 > 失败 > 大失败。
_OUTCOME_RANK = {
    "critical_success": 4,
    "hard_success": 3,
    "success": 2,
    "failure": 1,
    "fumble": 0,
}




def _resolve_check_actor(
    char_ref: str,
    skill_name: str,
    player_char: Character,
    teammates: list[Character] | None,
    module: Module,
) -> tuple[dict, str, bool, str | None]:
    """把 char= 解析成 (character_data, 显示名, is_npc, char_id)。

    空/主角→主角；队友名→对应队友；NPC 名→用模组 NPC 数值卡（缺该技能用 DEFAULT_NPC_SKILL
    兜底）。匹配不到时兜底当作主角，避免检定无法进行。char_id 为对应玩家角色的 id（NPC 为 None）。
    """
    name = (char_ref or "").strip()
    ref_kind, separator, ref_id = name.partition(":")
    stable_ref = separator == ":" and ref_kind in ("character", "npc") and bool(ref_id)

    def cdata_of(c: Character) -> dict:
        return {
            "base_attributes": c.base_attributes,
            "skills": c.skills,
            "system_data": c.system_data,
        }

    if (
        not name
        or name in ("主角", "玩家", player_char.name)
        or (ref_kind == "character" and ref_id == player_char.id)
    ):
        return cdata_of(player_char), player_char.name, False, player_char.id
    for t in (teammates or []):
        if t.name and (
            t.name == name or name in t.name or t.name in name
            or (ref_kind == "character" and ref_id == t.id)
        ):
            return cdata_of(t), t.name, False, t.id
    for npc in (module.npcs or []):
        nm = npc.get("name", "")
        npc_id = str(npc.get("id") or nm)
        if nm and (
            nm == name or name in nm or nm in name
            or (ref_kind == "npc" and ref_id == npc_id)
        ):
            skills = dict(npc.get("skills") or {})
            if skill_name and skill_name not in skills:
                skills[skill_name] = DEFAULT_NPC_SKILL
            return {"base_attributes": {}, "skills": skills, "system_data": {}}, nm, True, None
    if stable_ref:
        raise ValueError("所选检定对象已不在当前游戏中，请刷新后重选")
    return cdata_of(player_char), player_char.name, False, player_char.id


def _parse_bonus_penalty(kv: dict, prefix: str = "") -> tuple[int, int]:
    """解析奖励/惩罚骰数量；非法值按 0，负数取绝对值，并按 CoC 上限截为 2。"""
    def _n(key: str) -> int:
        raw = str(kv.get(f"{prefix}{key}") or "").strip()
        try:
            return min(abs(int(raw)), 2) if raw else 0
        except ValueError:
            return 0
    return _n("bonus"), _n("penalty")


def _check_dice_detail(result) -> dict:
    """把 CheckResult 的逐骰明细组装成前端契约的 dice 对象（kind=check）。

    供 3D 骰子动画严格还原：tens 含所有掷出的十位、tens_kept 是采用值、units 个位、
    bonus/penalty 数量。result 由 tens_kept + units 合成（十位00+个位0=100）。
    """
    return {
        "kind": "check",
        "result": result.roll,
        "tens": list(result.tens),
        "tens_kept": result.tens_kept,
        "units": result.units,
        "bonus": result.bonus,
        "penalty": result.penalty,
    }


def _pool_dice_detail(roll_result) -> dict:
    """把 DiceRollResult（NdM+K 骰池，如 SAN 损失/伤害）组装成契约的 dice 对象（kind=pool）。"""
    sides = 0
    m = re.match(r"\s*\d+d(\d+)", (roll_result.notation or "").strip().lower())
    if m:
        sides = int(m.group(1))
    return {
        "kind": "pool",
        "notation": roll_result.notation,
        "dice": [{"sides": sides, "value": v} for v in roll_result.rolls],
        "modifier": roll_result.modifier,
        "total": roll_result.total,
    }


def _exec_generic_roll(
    db: Session,
    session_id: str,
    module: Module,
    payload: dict,
) -> tuple[list[str], str]:
    """执行真人 KP 自定义骰池；暗投的持久事件和广播均不携带实际结果。"""
    try:
        count = int(payload.get("count") or 1)
        sides = int(payload.get("sides") or 6)
        modifier = int(payload.get("modifier") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("骰子数量、面数和修正值必须是整数") from error
    if not 1 <= count <= 20:
        raise ValueError("骰子数量必须在 1 到 20 之间")
    if not 2 <= sides <= 1000:
        raise ValueError("骰子面数必须在 2 到 1000 之间")
    if not -10000 <= modifier <= 10000:
        raise ValueError("骰子修正值必须在 -10000 到 10000 之间")

    notation = f"{count}d{sides}" + (f"{modifier:+d}" if modifier else "")
    result = get_engine(module.rule_system).roll_dice(notation)
    reason = str(payload.get("reason") or "临场裁定").strip() or "临场裁定"
    if len(reason) > 200:
        raise ValueError("掷骰用途不能超过 200 个字符")
    rolls_text = "、".join(str(value) for value in result.rolls)
    private_result = f"{reason}｜{notation}：[{rolls_text}]"
    if modifier:
        private_result += f" {modifier:+d}"
    private_result += f" = {result.total}"

    blind = str(payload.get("visibility") or "open").strip().lower() == "blind"
    if blind:
        public_content = f"KP 为“{reason}”进行了一次暗投（结果仅 KP 可见）"
        metadata = {
            "generic_roll": True, "blind": True, "reason": reason,
            "kp_manual": True,
        }
    else:
        public_content = f"KP 掷骰｜{private_result}"
        metadata = {
            "generic_roll": True, "reason": reason, "notation": notation,
            "rolls": list(result.rolls), "modifier": result.modifier,
            "total": result.total, "dice": _pool_dice_detail(result),
            "kp_manual": True,
        }
    event = session_service.add_event(
        db, session_id, "dice", public_content, actor_name="KP", metadata=metadata,
    )
    return [event_to_chunk(event)], private_result


_ALL_TOKENS = {"在场", "全体", "全部", "所有人", "所有", "all", "everyone"}
_GROUP_CHECK_WORDS = ("全员", "全体", "所有调查员", "所有角色", "每名角色", "每个角色", "所有人")


def _scene_requires_group_check(
    module: Module, game_session: GameSession, player_char: Character, skill_name: str,
) -> bool:
    """兼容旧模组 JSON：明文机制点明确写群检时，补全为在场群检。

    仅在当前场景恰好只有一个同技能 dice_check 机制点、且 trigger/note 明确出现群检词时
    才补全；显式 char/chars 由调用方优先保留，不经过此兜底。
    """
    scene_id = session_service.get_char_location(game_session, player_char.id) or game_session.current_scene_id
    scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
    events = (scene or {}).get("events") or []
    same_skill = [
        e for e in events
        if isinstance(e, dict)
        and e.get("kind") == "dice_check"
        and str(e.get("skill") or "").strip() == skill_name
    ]
    if len(same_skill) != 1:
        return False
    text = " ".join(str(same_skill[0].get(k) or "") for k in ("trigger", "note"))
    return any(word in text for word in _GROUP_CHECK_WORDS)


def _resolve_san_targets(
    chars_ref: str | None,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """把 SAN_CHECK 的 chars= 解析成目睹者角色列表（玩家方角色一视同仁，无主角特权）。

    空或「在场/全体/all」→ 全队；否则按名单（逗号/顿号分隔）匹配，匹配不到兜底全队。
    """
    party = [player_char] + list(teammates or [])
    ref = (chars_ref or "").strip()
    if not ref or ref.lower() in _ALL_TOKENS or ref in _ALL_TOKENS:
        return party
    names = [n.strip() for n in re.split(r"[,，、/]", ref) if n.strip()]
    out: list[Character] = []
    for n in names:
        for c in party:
            if c.name and (c.name == n or n in c.name or c.name in n) and c not in out:
                out.append(c)
    return out or party


def _present_party(
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """在场玩家角色 = 与主角同处一个场景的 player+teammates（公共/被动群检的默认目标）。

    未追踪位置（单场景游戏常见）→ 全队都在场。分头时只取与主角同场景的一组，
    避免让别处场景的角色也对这里的声响/线索检定。
    """
    party = [player_char] + list(teammates or [])
    locs = session_service.get_party_locations(game_session)
    if not locs:
        return party
    ref = locs.get(player_char.id) or game_session.current_scene_id
    present = [
        c for c in party
        if (locs.get(c.id) or game_session.current_scene_id) == ref
    ]
    return present or party


def _resolve_dice_group_targets(
    char_ref: str,
    group_ref: str,
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """群检目标：char=在场/全体 或 chars=在场 → 在场全体；chars=名单 → 具名成员。"""
    ref = (group_ref or char_ref or "").strip()
    if not ref or ref in _ALL_TOKENS or ref.lower() in _ALL_TOKENS:
        return _present_party(game_session, player_char, teammates)
    party = [player_char] + list(teammates or [])
    names = [n.strip() for n in re.split(r"[,，、/]", ref) if n.strip()]
    out: list[Character] = []
    for n in names:
        for c in party:
            if c.name and (c.name == n or n in c.name or c.name in n) and c not in out:
                out.append(c)
    return out or _present_party(game_session, player_char, teammates)


async def _resolve_opposed(
    db, session_id, kv, engine, module, player_char, teammates, dice_descriptions,
):
    """对抗骰：两方各投一次，比成功等级；同级比技能值高者胜，再平则平局。

    参数：a/b（或 actor/target）= 角色名；a_skill/b_skill（缺省取 skill）= 各自技能。
    """
    a_ref = (kv.get("a") or kv.get("actor") or "").strip()
    b_ref = (kv.get("b") or kv.get("target") or "").strip()
    a_skill = (kv.get("a_skill") or kv.get("skill") or "").strip()
    b_skill = (kv.get("b_skill") or a_skill).strip()
    if not a_ref or not b_ref or not a_skill or not b_skill:
        raise ValueError("对抗检定必须选择双方对象并填写双方技能")

    a_data, a_name, a_is_npc, a_id = _resolve_check_actor(
        a_ref, a_skill, player_char, teammates, module,
    )
    b_data, b_name, b_is_npc, b_id = _resolve_check_actor(
        b_ref, b_skill, player_char, teammates, module,
    )
    if (
        (a_id is not None and a_id == b_id)
        or (a_is_npc == b_is_npc and a_name == b_name)
    ):
        raise ValueError("对抗检定的双方不能是同一个对象")

    a_bonus, a_penalty = _parse_bonus_penalty(kv, "a_")
    b_bonus, b_penalty = _parse_bonus_penalty(kv, "b_")
    a_res = engine.resolve_check(
        a_data, a_skill, "normal", bonus=a_bonus, penalty=a_penalty,
    )
    b_res = engine.resolve_check(
        b_data, b_skill, "normal", bonus=b_bonus, penalty=b_penalty,
    )

    ar, br = _OUTCOME_RANK.get(a_res.outcome, 1), _OUTCOME_RANK.get(b_res.outcome, 1)
    if ar != br:
        winner = "attacker" if ar > br else "defender"
    elif a_res.skill_value != b_res.skill_value:
        winner = "attacker" if a_res.skill_value > b_res.skill_value else "defender"
    else:
        winner = None

    winner_name = a_name if winner == "attacker" else (b_name if winner == "defender" else "")
    verdict = f"{winner_name} 胜" if winner_name else "平局"
    private_content = (
        f"对抗骰　{a_name}（{a_skill}）{a_res.description}　vs　"
        f"{b_name}（{b_skill}）{b_res.description}　→　{verdict}"
    )
    opposed = {
        "attacker": {
            "name": a_name, "skill": a_skill, "roll": a_res.roll,
            "target": a_res.target, "outcome": a_res.outcome,
        },
        "defender": {
            "name": b_name, "skill": b_skill, "roll": b_res.roll,
            "target": b_res.target, "outcome": b_res.outcome,
        },
        "winner": winner,
        "result": verdict,
    }
    blind = (kv.get("visibility") or "open").strip().lower() == "blind"
    if blind:
        dice_content = f"KP 进行了一次对抗暗投：{a_name} vs {b_name}（结果仅 KP 可见）"
        dice_meta = {"opposed": True, "blind": True, "kp_manual": True}
    else:
        dice_content = private_content
        dice_meta = {
            "opposed": opposed,
            "a": {**opposed["attacker"], "actor": a_name, "dice": _check_dice_detail(a_res)},
            "b": {**opposed["defender"], "actor": b_name, "dice": _check_dice_detail(b_res)},
            "winner": winner_name or "平局",
            "kp_manual": True,
        }
    ev = session_service.add_event(
        db, session_id, "dice", dice_content, actor_name="系统", metadata=dice_meta,
    )
    yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id)
    dice_descriptions.append(
        f"【对抗暗投，仅 KP 可见】{private_content}" if blind else private_content
    )
