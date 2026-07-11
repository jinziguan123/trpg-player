"""战斗状态机（P2）：把纯引擎 combat.py 接到会话 world_state.combat 的生命周期上。

职责：建参战方 / 起止战斗 / 玩家行动结算 / 自动推进 NPC 回合（P2 用启发式，P3 接子代理）/
HP·重伤应用（玩家队友同步角色卡，敌人只在战斗态）/ 结果摘要。不含 LLM。

回合模型：战斗期间由**先攻队列**驱动（turn_index 指向当前行动者），覆盖非战斗的确认制。
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.session import GameSession
from app.rules.coc import combat as engine
from app.rules.coc.weapons import WEAPON_CATEGORY_ORDER
from app.services import session_service

# 火器大类（决定先攻火器优先与「远程」判定）
_FIREARM_CATEGORIES = {"手枪", "半自动步枪", "全自动步枪", "霰弹枪", "冲锋枪", "狙击步枪", "机枪"}
_ = WEAPON_CATEGORY_ORDER  # 引用以示来源（大类枚举取自武器表）


def _chunk(chunk_type: str, content: str = "", **extra) -> str:
    data = {"type": chunk_type, "content": content, **{k: v for k, v in extra.items() if v is not None}}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _weapon_is_firearm(weapon_name: str) -> bool:
    w = engine.resolve_weapon(weapon_name)
    return w.get("category") in _FIREARM_CATEGORIES or (w.get("skill", "").startswith("射击"))


def _char_participant(char: Character, side: str, is_human: bool = True) -> dict:
    """把玩家/队友角色卡转成参战方。HP 与状态与角色卡同步。is_human=True 的回合会停下等操作。"""
    sd = char.system_data or {}
    hp = (sd.get("hitPoints") or {})
    weapon = (sd.get("combat", {}) or {}).get("weapon") or "徒手格斗"
    return {
        "id": char.id, "char_id": char.id, "name": char.name, "side": side,
        "is_human": is_human,
        "dex": (char.base_attributes or {}).get("DEX", 50),
        "hp": hp.get("current", 0), "max_hp": hp.get("max", hp.get("current", 0)),
        "status": "ok", "weapon": weapon, "has_firearm": _weapon_is_firearm(weapon),
        "skills": char.skills or {}, "base_attributes": char.base_attributes or {},
        "system_data": sd, "db": (sd.get("damageBonus") or "0"),
        "acted_this_round": False, "dodged_this_round": False,
    }


def _npc_participant(npc: dict, side: str = "enemy") -> dict:
    """把模组 NPC（或临时敌）转成参战方。HP 由属性派生，只在战斗态追踪。"""
    attrs = npc.get("attributes") or {}
    con, siz = attrs.get("CON", 50), attrs.get("SIZ", 50)
    max_hp = npc.get("hp") or (con + siz) // 10
    weapon = npc.get("weapon") or "徒手格斗"
    from app.rules.coc.character import compute_derived
    db = "0"
    try:
        db = compute_derived(attrs).get("damageBonus", "0")
    except Exception:
        pass
    return {
        "id": npc.get("id") or f"enemy_{uuid.uuid4().hex[:6]}",
        "npc_id": npc.get("id"), "name": npc.get("name") or "敌人", "side": side,
        "dex": attrs.get("DEX", 50), "hp": max_hp, "max_hp": max_hp, "status": "ok",
        "weapon": weapon, "has_firearm": _weapon_is_firearm(weapon),
        "skills": npc.get("skills") or {}, "base_attributes": attrs, "system_data": {},
        "db": db, "combat_ai": npc.get("combat_ai"),
        # 有性格/秘密的关键 NPC → 战斗中走子代理决策+叙述；杂兵走启发式
        "is_key": bool(npc.get("personality") or npc.get("secrets")),
        "personality": npc.get("personality"),
        "acted_this_round": False, "dodged_this_round": False,
    }


def _char_data(p: dict) -> dict:
    """参战方 → resolve_skill_check 需要的 character_data。"""
    return {"skills": p.get("skills") or {}, "base_attributes": p.get("base_attributes") or {},
            "system_data": p.get("system_data") or {}}


def get_combat(session: GameSession) -> dict | None:
    c = (session.world_state or {}).get("combat")
    return c if c and c.get("active") else None


def _save_combat(db: Session, session_id: str, state: dict | None) -> None:
    session = db.get(GameSession, session_id)
    ws = dict(session.world_state or {})
    if state is None:
        ws.pop("combat", None)
    else:
        ws["combat"] = state
    session.world_state = ws
    db.commit()


def start_combat(db: Session, session_id: str, player_side: list[dict], enemies: list[dict],
                 trigger: str = "") -> dict:
    """建立战斗态：合并双方为参战方、排先攻、round=1。player_side/enemies 已是参战方 dict。"""
    participants = list(player_side) + list(enemies)
    order = engine.roll_initiative(participants)
    state = {
        "active": True, "round": 1, "turn_index": 0, "initiative": order,
        "log": [], "trigger": trigger, "flee_to_chase": None,
    }
    _save_combat(db, session_id, state)
    return state


async def start(db: Session, session_id: str, party: list[Character], enemies: list[dict],
                human_ids: set[str], trigger: str = "", agent=None, scene_hint: str = "") -> tuple[dict, list[str]]:
    """高层入口：从角色/敌人 spec 建参战方、起战斗、自动跑到第一个真人回合。返回 (state, chunks)。

    party：玩家方角色（human_ids 里的算真人玩家=会停下等操作，其余算 AI 队友=自动）。
    enemies：敌方 spec（含 attributes/skills/weapon/combat_ai/personality）。有 agent 时叙述开场交战。
    """
    player_side = [
        _char_participant(c, "player" if c.id in human_ids else "ally", c.id in human_ids)
        for c in party
    ]
    enemy_side = [_npc_participant(n, "enemy") for n in enemies]
    state = start_combat(db, session_id, player_side, enemy_side, trigger)
    chunks = [_chunk("combat_start", trigger or "战斗爆发！", metadata=_combat_meta(state))]
    drive_chunks, beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            chunks.append(_combat_narration(db, session_id, prose))
    return state, chunks + drive_chunks


def _combat_meta(state: dict) -> dict:
    actor = current_actor(state)
    return {
        "round": state.get("round"),
        "turn": actor.get("id") if actor else None,
        "order": [{"id": p["id"], "name": p["name"], "side": p["side"], "is_human": p.get("is_human", False),
                   "hp": p["hp"], "max_hp": p["max_hp"], "status": p["status"]}
                  for p in state.get("initiative") or []],
    }


def current_actor(state: dict) -> dict | None:
    order = state.get("initiative") or []
    i = state.get("turn_index", 0)
    return order[i] if 0 <= i < len(order) else None


def _find(state: dict, pid: str) -> dict | None:
    return next((p for p in state.get("initiative") or [] if p.get("id") == pid), None)


def apply_damage(db: Session, state: dict, target: dict, amount: int, reason: str) -> list[str]:
    """对参战方结算伤害：更新战斗态 HP/状态；玩家/队友同步角色卡 HP + 重伤体质检定判昏迷。
    返回可读结算行（供叙述/日志）。"""
    r = engine.resolve_wound(target.get("hp", 0), target.get("max_hp") or 1, amount, _char_data(target))
    target["hp"] = r["new_hp"]
    target["status"] = r["status"]
    # r["lines"] 首行是「受到 N 点伤害（HP a→b）」，冠上名字与 reason，保持既有可读格式
    lines: list[str] = []
    for i, line in enumerate(r["lines"]):
        text = f"{target['name']} {line}"
        if i == 0 and reason:
            text += f"——{reason}"
        lines.append(text)

    # 玩家/队友：把 HP 与状态写回角色卡（敌人只在战斗态）
    if target.get("char_id"):
        char = db.get(Character, target["char_id"])
        if char:
            sd = dict(char.system_data or {})
            hp = dict(sd.get("hitPoints") or {})
            hp["current"] = target["hp"]
            sd["hitPoints"] = hp
            char.system_data = sd
            if target["status"] in ("dead", "dying", "unconscious", "major_wound"):
                char.status = target["status"]
            db.add(char)
            db.commit()
    return lines


async def resolve_player_action(
    db: Session, session_id: str, actor_id: str, action: dict,
    agent=None, scene_hint: str = "",
) -> list[str]:
    """结算当前玩家参战方的一个行动，返回广播 chunks。行动: {type, target_id?, weapon?}。

    仅当 actor_id == 当前先攻行动者时有效（先攻队列强制）。结算后推进、自动跑随后的 NPC 回合
    （关键 NPC 走 agent.decide，杂兵走启发式），停在下一个玩家回合或战斗结束。
    有 agent 时把本轮交战整段交给战斗子代理叙述（一次调用），叙述 chunk 置于机械 chunk 之前。
    """
    session = db.get(GameSession, session_id)
    state = get_combat(session)
    if not state:
        raise ValueError("当前不在战斗中")
    actor = current_actor(state)
    if not actor or actor.get("id") != actor_id:
        raise ValueError("现在不是你的先攻回合")

    chunks, summary = _apply_one_action(db, session_id, state, actor, action)
    beats = [summary] if summary else []
    actor["acted_this_round"] = True
    engine.advance_turn(state)
    drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    beats += drive_beats

    out: list[str] = []
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            out.append(_combat_narration(db, session_id, prose))
    return out + chunks + drive_chunks


async def resolve_reaction(db: Session, session_id: str, defender_id: str, choice: str,
                           weapon: str | None = None, agent=None, scene_hint: str = "") -> list[str]:
    """真人对一次针对自己的攻击做出反应（fight_back/dodge/cover），结算这一击后续跑驱动。"""
    session = db.get(GameSession, session_id)
    state = get_combat(session)
    if not state:
        raise ValueError("当前不在战斗中")
    pr = state.get("pending_reaction")
    if not pr or pr["defender_id"] != defender_id:
        raise ValueError("现在没有等待你的反应")
    if choice not in pr["allowed"]:
        raise ValueError("该反应在本次攻击下不可用")
    attacker = _find(state, pr["attacker_id"])
    defender = _find(state, pr["defender_id"])
    out: list[str] = []
    beats: list[str] = []
    if attacker and defender and engine.is_active(attacker) and engine.is_active(defender):
        res = engine.resolve_attack(
            _char_data(attacker), attacker.get("db", "0"), pr["weapon"],
            defender_data=_char_data(defender), defense=choice, ranged=pr["ranged"],
        )
        out.append(_combat_dice(db, session_id, attacker, defender, pr["weapon"], res))
        if res["hit"] and res["damage"]:
            victim = defender if res["damage_to"] == "defender" else attacker
            for line in apply_damage(db, state, victim, res["damage"]["total"],
                                     reason=f"{attacker['name']} 的 {pr['weapon']}"):
                out.append(_combat_line(db, session_id, line))
        verb = {"fight_back": "反击", "dodge": "闪避", "cover": "扑向掩体"}.get(choice, choice)
        beats.append(f"{defender['name']} 对 {attacker['name']} 的攻击选择{verb}："
                     + ("被击中" if (res["hit"] and res["damage_to"] == "defender")
                        else "反击得手" if (res["hit"] and res["damage_to"] == "attacker")
                        else "未受伤"))
    state["pending_reaction"] = None
    if attacker:
        attacker["acted_this_round"] = True
    engine.advance_turn(state)
    drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    beats += drive_beats
    narr: list[str] = []
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            narr.append(_combat_narration(db, session_id, prose))
    return narr + out + drive_chunks


def _apply_one_action(db: Session, session_id: str, state: dict, actor: dict, action: dict) -> tuple[list[str], str]:
    """结算某参战方的一个行动（玩家/NPC 共用）。返回 (chunks, 机械结算摘要行 供子代理叙述)。"""
    chunks: list[str] = []
    atype = action.get("type") or action.get("action") or "other"

    if atype == "flee":
        actor["status"] = "fled"
        summary = f"{actor['name']} 脱离战斗、转身逃走"
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "flee"})
        chunks.append(_combat_line(db, session_id, summary + "。"))
        return chunks, summary

    if atype in ("dodge", "wait", "other"):
        summary = {"dodge": f"{actor['name']} 摆出防御姿态", "wait": f"{actor['name']} 按兵不动"}.get(
            atype, f"{actor['name']} 采取了行动")
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": atype})
        chunks.append(_combat_line(db, session_id, summary + "。"))
        return chunks, summary

    # attack
    target = _find(state, action.get("target_id"))
    if target is None or not engine.is_active(target):
        s = f"{actor['name']} 的目标已不在场"
        chunks.append(_combat_line(db, session_id, s + "。"))
        return chunks, s
    weapon = action.get("weapon") or actor.get("weapon") or "徒手格斗"
    ranged = _weapon_is_firearm(weapon)
    defense = None if ranged else (action.get("defense") or engine.heuristic_defense(target, is_firearm=False))
    res = engine.resolve_attack(
        _char_data(actor), actor.get("db", "0"), weapon,
        defender_data=_char_data(target), defense=defense, ranged=ranged,
    )
    chunks.append(_combat_dice(db, session_id, actor, target, weapon, res))
    dmg_lines: list[str] = []
    if res["hit"] and res["damage"]:
        victim = target if res["damage_to"] == "defender" else actor
        dmg_lines = apply_damage(db, state, victim, res["damage"]["total"],
                                 reason=f"{actor['name']} 的 {weapon}")
        for line in dmg_lines:
            chunks.append(_combat_line(db, session_id, line))
    state["log"].append({
        "round": state["round"], "actor": actor["name"], "action": "attack",
        "target": target["name"], "hit": res["hit"],
        "damage": (res["damage"] or {}).get("total") if res["hit"] else 0,
    })
    verb = "命中" if res["hit"] else "未命中/被防住"
    summary = f"{actor['name']} 用 {weapon} 攻击 {target['name']}：{verb}" + (
        "；" + "；".join(dmg_lines) if dmg_lines else "")
    return chunks, summary


async def drive_npcs(db: Session, session_id: str, state: dict, agent=None, scene_hint: str = "") -> tuple[list[str], list[str]]:
    """从**当前行动者**起自动跑非真人回合（关键 NPC 走 agent.decide，杂兵走启发式），停在真人回合
    或战斗结束。返回 (chunks, 各回合摘要行)。"""
    chunks: list[str] = []
    beats: list[str] = []
    for _ in range(64):  # 安全上限，防死循环
        end = engine.check_combat_end(state.get("initiative") or [])
        if end:
            chunks += _end_combat(db, session_id, state, end)
            return chunks, beats
        actor = current_actor(state)
        if actor is None:
            break
        # 濒死者回合开始先跑体质 tick（须在 is_active 跳过之前，否则永远不掷）
        if actor.get("status") == "dying":
            for line in engine.tick_dying(actor):
                chunks.append(_combat_line(db, session_id, line))
            _save_combat(db, session_id, state)
        if not engine.is_active(actor):   # 当前指针落在失能者 → 跳过
            engine.advance_turn(state)
            continue
        _save_combat(db, session_id, state)
        chunks.append(_combat_state_chunk(state))
        if actor.get("is_human"):
            return chunks, beats   # 停下等真人操作
        # 关键 NPC 走子代理决策；失败/杂兵回落启发式
        action = None
        if agent and actor.get("is_key"):
            action = _validate_npc_action(state, actor, await agent.decide(state, actor, scene_hint))
        if action is None:
            action = engine.heuristic_npc_action(state, actor)
        # 攻击真人防御者 → 暂停驱动，落 pending_reaction、广播提示，等真人经端点回选择
        atype = action.get("type") or action.get("action")
        if atype == "attack":
            target = _find(state, action.get("target_id"))
            if target and engine.is_active(target) and target.get("is_human"):
                weapon = action.get("weapon") or actor.get("weapon") or "徒手格斗"
                is_fire = _weapon_is_firearm(weapon)
                state["pending_reaction"] = {
                    "attacker_id": actor["id"], "defender_id": target["id"],
                    "weapon": weapon, "ranged": is_fire,
                    "allowed": engine.allowed_reactions(is_fire),
                }
                _save_combat(db, session_id, state)
                chunks.append(_chunk("combat_reaction_prompt", metadata={
                    **state["pending_reaction"],
                    "attacker_name": actor["name"], "defender_name": target["name"],
                }))
                return chunks, beats   # 暂停驱动，不结算、不推进
        c, s = _apply_one_action(db, session_id, state, actor, action)
        chunks += c
        if s:
            beats.append(s)
        actor["acted_this_round"] = True
        engine.advance_turn(state)
    _save_combat(db, session_id, state)
    return chunks, beats


def _validate_npc_action(state: dict, actor: dict, action: dict | None) -> dict | None:
    """校正子代理给的行动：attack 目标须存活且属敌对方；非法则丢弃（回落启发式）。"""
    if not action or action.get("action") not in ("attack", "flee", "dodge"):
        return None
    if action.get("action") != "attack":
        return {"type": action["action"]}
    target = _find(state, action.get("target_id"))
    hostile = target and engine.is_active(target) and target.get("side") != actor.get("side")
    if not hostile:
        return None
    return {"type": "attack", "target_id": action["target_id"],
            "weapon": action.get("weapon") or actor.get("weapon") or "徒手格斗"}


def add_combatant(db: Session, session_id: str, spec: dict, side: str = "enemy") -> dict:
    """中途加入敌/友，按先攻插入当前序（下一轮重排时归位）。"""
    session = db.get(GameSession, session_id)
    state = get_combat(session)
    if not state:
        raise ValueError("当前不在战斗中")
    p = _npc_participant(spec, side) if side == "enemy" or not spec.get("char_id") else spec
    state["initiative"] = engine.roll_initiative((state["initiative"] or []) + [p])
    _save_combat(db, session_id, state)
    return state


def _combat_summary(state: dict, outcome: str) -> dict:
    parts = state.get("initiative") or []
    return {
        "outcome": outcome,
        "casualties": [{"name": p["name"], "status": p["status"]}
                       for p in parts if p["status"] in ("dead", "dying", "unconscious", "fled")],
        "hp_after": {p["name"]: p["hp"] for p in parts},
        "rounds": state.get("round", 1),
    }


def _end_combat(db: Session, session_id: str, state: dict, outcome: str) -> list[str]:
    """结束战斗：产出结果摘要存 world_state.combat_result，清 combat 态。返回收尾 chunks。"""
    summary = _combat_summary(state, outcome)
    session = db.get(GameSession, session_id)
    ws = dict(session.world_state or {})
    ws["combat_result"] = summary
    ws.pop("combat", None)
    session.world_state = ws
    db.commit()
    label = {"players_win": "战斗结束：敌方被击溃。", "players_defeated": "战斗结束：调查员一方倒下。",
             "no_combatants": "战斗结束。"}.get(outcome, "战斗结束。")
    return [_combat_line(db, session_id, label), _chunk("combat_end", label, metadata=summary)]


# ── 落库 + 广播小工具 ────────────────────────────────────────────────

def _combat_line(db: Session, session_id: str, text: str) -> str:
    ev = session_service.add_event(db, session_id, "system", text, actor_name="战斗")
    return _chunk("system", text, id=ev.id)


def _combat_narration(db: Session, session_id: str, text: str) -> str:
    """战斗子代理的一段叙述：落成 narration 事件（进历史/复盘），返回广播 chunk。"""
    ev = session_service.add_event(db, session_id, "narration", text, actor_name="KP")
    return _chunk("narration_full", text, id=ev.id, actor_name="KP")


def _combat_dice(db: Session, session_id: str, actor: dict, target: dict, weapon: str, res: dict) -> str:
    atk = res["attacker_check"]
    dfn = res.get("defender_check")
    if dfn is not None:
        content = (f"{actor['name']}（{weapon}）{atk.description} vs {target['name']} {dfn.description}"
                   f" → {'命中' if res['hit'] else '未命中'}")
    else:
        content = f"{actor['name']}（{weapon}）{atk.description} → {'命中' if res['hit'] else '未命中'}"
    ev = session_service.add_event(db, session_id, "dice", content, actor_name="战斗",
                                   metadata={"combat_attack": True, "hit": res["hit"]})
    return _chunk("dice", content, id=ev.id)


def _combat_state_chunk(state: dict) -> str:
    """把当前战斗态（回合/先攻序/各方HP/轮到谁）广播给前端渲染战斗视图。"""
    return _chunk("combat_state", metadata=_combat_meta(state))
