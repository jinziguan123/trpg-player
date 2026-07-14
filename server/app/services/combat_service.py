"""战斗状态机（P2）：把纯引擎 combat.py 接到会话 world_state.combat 的生命周期上。

职责：建参战方 / 起止战斗 / 玩家行动结算 / 自动推进 NPC 回合（P2 用启发式，P3 接子代理）/
HP·重伤应用（玩家队友同步角色卡，敌人只在战斗态）/ 结果摘要。不含 LLM。

回合模型：战斗期间由**先攻队列**驱动（turn_index 指向当前行动者），覆盖非战斗的确认制。
"""

from __future__ import annotations

import copy
import json
import re
import uuid

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.session import GameSession
from app.rules.coc import combat as engine
from app.rules.coc.weapons import WEAPON_CATEGORY_ORDER
from app.services import session_service
from app.services.room_hub import room_hub

# 火器大类（决定先攻火器优先与「远程」判定）
_FIREARM_CATEGORIES = {"手枪", "半自动步枪", "全自动步枪", "霰弹枪", "冲锋枪", "狙击步枪", "机枪"}
_ = WEAPON_CATEGORY_ORDER  # 引用以示来源（大类枚举取自武器表）


def _chunk(chunk_type: str, content: str = "", **extra) -> str:
    data = {"type": chunk_type, "content": content, **{k: v for k, v in extra.items() if v is not None}}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _weapon_is_firearm(weapon_name: str) -> bool:
    w = engine.resolve_weapon(weapon_name)
    return w.get("category") in _FIREARM_CATEGORIES or (w.get("skill", "").startswith("射击"))


def _coerce_armor(v) -> int:
    """护甲值宽松解析成非负整数（缺省/非法 → 0）。角色卡/NPC 设定里护甲可能填成数字或字符串。"""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


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
        "armor": _coerce_armor(sd.get("armor")),   # 护甲值：每次受到的物理伤害先扣它（CoC7e）
        "acted_this_round": False, "dodged_this_round": False,
        # P2 主动动作集：正交条件 / 瞄准 / 该处伤是否已急救（RAW 每处伤一次）
        "conditions": [], "aim": False, "first_aid_used": False,
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
        "db": db, "armor": _coerce_armor(npc.get("armor")), "combat_ai": npc.get("combat_ai"),
        # 有性格/秘密的关键 NPC → 战斗中走子代理决策+叙述；杂兵走启发式
        "is_key": bool(npc.get("personality") or npc.get("secrets")),
        "personality": npc.get("personality"),
        "acted_this_round": False, "dodged_this_round": False,
        # P2 主动动作集：正交条件 / 瞄准 / 该处伤是否已急救
        "conditions": [], "aim": False, "first_aid_used": False,
    }


def _char_data(p: dict) -> dict:
    """参战方 → resolve_skill_check 需要的 character_data。"""
    return {"skills": p.get("skills") or {}, "base_attributes": p.get("base_attributes") or {},
            "system_data": p.get("system_data") or {}}


def get_combat(session: GameSession) -> dict | None:
    c = (session.world_state or {}).get("combat")
    if not (c and c.get("active")):
        return None
    # 深拷贝：调用方在返回的 state 上原地改动（如挂 pending_roll）不会触及 ORM 挂着的 JSON 值——
    # 否则「改旧值 + _save_combat 整体赋值」会被 SQLAlchemy 判为无变化而不落库（提交后重载即丢）。
    return copy.deepcopy(c)


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
    # 本场起点：开打前会话最大事件 seq（= 下一个 seq - 1）。一个会话可有多场战斗，落库的
    # combat_log 事件本身无场次边界；前端据此让日志抽屉只收本场（seq > started_seq）的结算行，
    # 重连（走 GET /combat 恢复、不经 combat_start 分支）也能拿到边界、不掺上一场。
    started_seq = session_service.get_next_sequence_num(db, session_id) - 1
    state = {
        "active": True, "round": 1, "turn_index": 0, "initiative": order,
        "log": [], "trigger": trigger, "flee_to_chase": None, "started_seq": started_seq,
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
                   "hp": p["hp"], "max_hp": p["max_hp"], "status": p["status"], "weapon": p.get("weapon"),
                   "armor": int(p.get("armor") or 0),   # 护甲值 → 前端 HUD 护甲徽标
                   # P2 正交条件（grappled/disarmed）与瞄准态 → 前端 HUD 徽标（被擒/缴械/瞄准中）
                   "conditions": list(p.get("conditions") or []), "aim": bool(p.get("aim"))}
                  for p in state.get("initiative") or []],
        # 断线重连恢复：若正等某真人反应，带上 pending_reaction 让前端重弹反应提示。
        "pending_reaction": state.get("pending_reaction"),
        # 两段式投骰：命中后等该真人亲自掷伤害时，带上 pending_roll 让前端弹「投掷伤害」并重连恢复。
        "pending_roll": _pending_roll_public(state.get("pending_roll")),
        # 本场战斗日志起点 seq：前端据此让日志抽屉只收本场结算行（重连也能拿到边界，防掺上一场）。
        "started_seq": state.get("started_seq"),
    }


def _pending_roll_public(pr: dict | None) -> dict | None:
    """给前端的 pending_roll 投影：只暴露谁该投、投什么、提示文案；不泄露预定的伤害点数。"""
    if not pr:
        return None
    return {"actor_id": pr.get("actor_id"), "kind": pr.get("kind"),
            "label": pr.get("label"), "victim_id": pr.get("victim_id")}


def current_actor(state: dict) -> dict | None:
    order = state.get("initiative") or []
    i = state.get("turn_index", 0)
    return order[i] if 0 <= i < len(order) else None


def _find(state: dict, pid: str) -> dict | None:
    return next((p for p in state.get("initiative") or [] if p.get("id") == pid), None)


def apply_damage(db: Session, state: dict, target: dict, amount: int, reason: str,
                 ignore_armor: bool = False) -> list[str]:
    """对参战方结算伤害：更新战斗态 HP/状态；玩家/队友同步角色卡 HP + 重伤体质检定判昏迷。
    返回可读结算行（供叙述/日志）。

    护甲（CoC7e）：物理伤害先扣目标护甲值再入血，重伤阈值也按扣减后的净伤判。
    ignore_armor=True 用于火焰/持续燃烧等能量伤害（护甲挡不住）。
    """
    absorbed = 0
    armor = 0 if ignore_armor else int(target.get("armor") or 0)
    if armor > 0 and amount > 0:
        absorbed = min(amount, armor)
        amount = amount - absorbed
    r = engine.resolve_wound(target.get("hp", 0), target.get("max_hp") or 1, amount, _char_data(target))
    target["hp"] = r["new_hp"]
    target["status"] = r["status"]
    # 新伤 = 新的急救机会：重置 first_aid_used，否则前期被急救过的人再受伤（乃至濒死）
    # 会被 first_aid 分支顶端的 used 检查锁死，连救命的濒死稳住都做不了。
    if amount > 0:
        target["first_aid_used"] = False
    # r["lines"] 首行是「受到 N 点伤害（HP a→b）」，冠上名字与 reason，保持既有可读格式
    lines: list[str] = []
    for i, line in enumerate(r["lines"]):
        text = f"{target['name']} {line}"
        if i == 0:
            if absorbed > 0:
                text += f"（护甲挡下 {absorbed} 点）"
            if reason:
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


def _sync_char_status(db: Session, target: dict) -> None:
    """把参战方的 status 写回其角色卡（仅玩家/队友；敌人只在战斗态追踪）。"""
    if not target.get("char_id"):
        return
    char = db.get(Character, target["char_id"])
    if char and target["status"] in ("dead", "dying", "unconscious", "major_wound", "ok"):
        char.status = target["status"]
        db.add(char)
        db.commit()


def apply_heal(db: Session, state: dict, target: dict, amount: int) -> list[str]:
    """对参战方回血：更新战斗态 HP（封顶 max_hp）；玩家/队友同步写回角色卡。返回可读结算行。
    对称 apply_damage，但不触发状态迁移（濒死稳住由 first_aid 分支单独处理）。"""
    old = target.get("hp", 0)
    max_hp = target.get("max_hp") or old
    new_hp = min(max_hp, old + max(0, amount))
    target["hp"] = new_hp
    lines = [f"{target['name']} 恢复 {new_hp - old} 点伤势（HP {old}→{new_hp}）"]
    if target.get("char_id"):
        char = db.get(Character, target["char_id"])
        if char:
            sd = dict(char.system_data or {})
            hp = dict(sd.get("hitPoints") or {})
            hp["current"] = new_hp
            sd["hitPoints"] = hp
            char.system_data = sd
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
    if state.get("pending_roll"):
        raise ValueError("请先完成待掷的骰子")
    actor = current_actor(state)
    if not actor or actor.get("id") != actor_id:
        raise ValueError("现在不是你的先攻回合")

    # 两段式：真人攻击命中后由玩家亲自掷伤害（连命中判定也是玩家触发的这一掷）。
    atype = action.get("type") or action.get("action") or "other"
    if atype == "attack" and actor.get("is_human"):
        # 连发：shots 给出每发目标 → 连射入口统一处理（≥2 发一次性多枪结算、否则内部降级为单发两段式）。
        if any(action.get("shots") or []):
            return await _begin_player_burst(db, session_id, state, actor, action, agent, scene_hint)
        return await _begin_player_attack(db, session_id, state, actor, action, agent, scene_hint)

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


async def _begin_player_attack(
    db: Session, session_id: str, state: dict, actor: dict, action: dict,
    agent=None, scene_hint: str = "",
) -> list[str]:
    """真人攻击第一段：掷命中（3D 骰事件，玩家点『攻击』即触发）。命中→挂 pending_roll 等玩家掷伤害；
    未命中/目标已不在→照常记账、推进、驱动 NPC。"""
    target = _find(state, action.get("target_id"))
    if target is None or not engine.is_active(target):
        chunks, summary = _apply_one_action(db, session_id, state, actor, action)  # 目标没了→降级
        actor["acted_this_round"] = True
        engine.advance_turn(state)
        drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
        out: list[str] = []
        if agent and (([summary] if summary else []) + drive_beats):
            prose = await agent.narrate(state, ([summary] if summary else []) + drive_beats, scene_hint)
            if prose:
                out.append(_combat_narration(db, session_id, prose))
        return out + chunks + drive_chunks

    weapon = action.get("weapon") or actor.get("weapon") or "徒手格斗"
    disarmed = "disarmed" in (actor.get("conditions") or [])
    if disarmed:
        weapon = "徒手格斗"
    ranged = _weapon_is_firearm(weapon)
    aim_bonus = 1 if actor.get("aim") else 0
    target_grappled = "grappled" in (target.get("conditions") or [])
    defense = None if ranged else (
        action.get("defense")
        or engine.heuristic_defense(target, is_firearm=False, defender_grappled=target_grappled))
    res = engine.resolve_attack(
        _char_data(actor), actor.get("db", "0"), weapon,
        defender_data=_char_data(target), defense=defense, ranged=ranged,
        attacker_disarmed=disarmed, bonus=aim_bonus,
    )
    if actor.get("aim"):
        actor["aim"] = False   # 瞄准一次性消费

    # 第一段：把命中检定作为 3D 骰事件下发（玩家亲手触发的这一掷），并附对抗卡数据
    hit_content = _combat_dice_content(actor, target, weapon, res)
    out = [_combat_roll_event(db, session_id, hit_content, _hit_dice_detail(res["attacker_check"]),
                              opposed=_opposed_detail(res, actor["name"], target["name"]))]

    if res["hit"] and res["damage"] and res.get("damage_to"):
        # 命中 → 挂 pending_roll，等玩家亲自掷伤害；不立即扣血、不推进先攻。
        victim = target if res["damage_to"] == "defender" else actor
        state["pending_roll"] = {
            "id": uuid.uuid4().hex, "actor_id": actor["id"], "kind": "damage",
            "victim_id": victim["id"], "weapon": weapon,
            "damage": {"total": res["damage"]["total"], "rolls": list(res["damage"]["rolls"]),
                       "notation": res["damage"]["notation"],
                       "flags": list(res["damage"].get("flags") or [])},
            "reason": f"{actor['name']} 的 {weapon}",
            "label": f"投掷伤害（{res['damage']['notation']}）",
        }
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "attack_hit",
                             "target": target["name"], "hit": True})
        _save_combat(db, session_id, state)
        out.append(_combat_state_chunk(state))   # 带 pending_roll → 前端弹「投掷伤害」
        return out

    # 未命中：记账、推进、驱动
    state["log"].append({"round": state["round"], "actor": actor["name"], "action": "attack",
                         "target": target["name"], "hit": False, "damage": 0})
    actor["acted_this_round"] = True
    engine.advance_turn(state)
    drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    beats = [f"{actor['name']} 用 {weapon} 攻击 {target['name']}：未命中/被防住"] + drive_beats
    narr: list[str] = []
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            narr.append(_combat_narration(db, session_id, prose))
    return narr + out + drive_chunks


def _combat_burst_event(db: Session, session_id: str, actor: dict, weapon: str,
                        shots: list[dict]) -> str:
    """连发结果事件：一条 dice 事件带 metadata.burst（每发命中/伤害/惩罚骰）→ 前端「连射卡」。
    不带 metadata.dice（不逐发触发 3D 骰动画，连射是一次性泼弹、整体展示）。"""
    hits = sum(1 for s in shots if s.get("hit"))
    content = f"{actor['name']}（{weapon}）连射 {len(shots)} 发 → 命中 {hits} 发"
    meta = {"combat_burst": True, "weapon": weapon, "shots": shots}
    ev = session_service.add_event(db, session_id, "dice", content, actor_name="战斗", metadata=meta)
    chunk = _chunk("dice", content, id=ev.id, metadata=meta)
    room_hub.broadcast(session_id, chunk)
    return chunk


async def _begin_player_burst(
    db: Session, session_id: str, state: dict, actor: dict, action: dict,
    agent=None, scene_hint: str = "",
) -> list[str]:
    """真人连发（半自动/连发火器一轮多枪）：按射速上限截发，逐发结算命中/伤害，
    **换目标每换一个 +1 惩罚骰**（同目标连开不加罚，CoC7e RAW）。一次性结算、整体展示，
    不走两段式手掷伤害（连射是泼弹、不逐发仪式）。命中即扣血（走护甲）。"""
    weapon = action.get("weapon") or actor.get("weapon") or "徒手格斗"
    cap = engine.burst_capacity(weapon)
    shots = [t for t in (action.get("shots") or []) if t][:cap]
    # 前置校验：需可连射的火器（cap≥2）且至少 2 发；否则降级为单发两段式攻击。
    if cap < 2 or not _weapon_is_firearm(weapon) or len(shots) < 2:
        single = {k: v for k, v in action.items() if k != "shots"}
        single["target_id"] = shots[0] if shots else action.get("target_id")
        return await _begin_player_attack(db, session_id, state, actor, single, agent, scene_hint)

    aim_bonus = 1 if actor.get("aim") else 0
    if actor.get("aim"):
        actor["aim"] = False   # 瞄准一次性消费
    order_seen: dict[str, int] = {}
    shot_results: list[dict] = []
    out: list[str] = []
    for tid in shots:
        if tid not in order_seen:
            order_seen[tid] = len(order_seen)   # 目标首次出现顺序 = 该目标的换目标惩罚骰数
        pen = order_seen[tid]
        target = _find(state, tid)
        if target is None or not engine.is_active(target):
            shot_results.append({"target": target["name"] if target else "已倒下目标",
                                 "hit": False, "penalty": pen, "damage": None, "gone": True})
            continue
        res = engine.resolve_attack(_char_data(actor), actor.get("db", "0"), weapon,
                                    defender_data=None, ranged=True, bonus=aim_bonus, penalty=pen)
        chk = res["attacker_check"]
        rec = {"target": target["name"], "roll": chk.roll, "target_val": chk.skill_value,
               "outcome": chk.outcome, "hit": res["hit"], "penalty": pen,
               "damage": None, "flags": []}
        if res["hit"] and res["damage"]:
            rec["damage"] = res["damage"]["total"]
            rec["flags"] = list(res["damage"].get("flags") or [])
            for line in apply_damage(db, state, target, res["damage"]["total"],
                                     reason=f"{actor['name']} 连射（{weapon}）"):
                out.append(_combat_line(db, session_id, line))
        shot_results.append(rec)

    hits = sum(1 for r in shot_results if r.get("hit"))
    out.insert(0, _combat_burst_event(db, session_id, actor, weapon, shot_results))
    actor["acted_this_round"] = True
    engine.advance_turn(state)
    _save_combat(db, session_id, state)   # 先落库（推进）再驱动，避免覆盖 drive 里的结束清场
    drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    beats = [f"{actor['name']} 用 {weapon} 连射 {len(shots)} 发，命中 {hits} 发"] + drive_beats
    narr: list[str] = []
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            narr.append(_combat_narration(db, session_id, prose))
    return narr + out + drive_chunks


async def resolve_combat_roll(
    db: Session, session_id: str, actor_id: str, agent=None, scene_hint: str = "",
) -> list[str]:
    """真人攻击第二段：玩家亲自掷伤害（3D 骰事件）→ 扣血、推进先攻、续跑 NPC 驱动。"""
    session = db.get(GameSession, session_id)
    state = get_combat(session)
    if not state:
        raise ValueError("当前不在战斗中")
    pr = state.get("pending_roll")
    if not pr or pr.get("actor_id") != actor_id:
        raise ValueError("现在没有等待你的投掷")
    actor = _find(state, actor_id)
    if pr.get("kind") == "aoe_damage":
        return await _resolve_aoe_roll(db, session_id, state, actor, pr, agent, scene_hint)
    victim = _find(state, pr["victim_id"])
    weapon = pr.get("weapon") or "徒手格斗"

    dmg = pr["damage"]
    dmg_content = (f"{actor['name'] if actor else '攻击者'}（{weapon}）伤害 "
                   f"{dmg['notation']} = {dmg['total']}")
    out = [_combat_roll_event(db, session_id, dmg_content, _damage_dice_detail(dmg))]

    hit_beat = ""
    if victim and engine.is_active(victim):
        for line in apply_damage(db, state, victim, dmg["total"], reason=pr["reason"]):
            out.append(_combat_line(db, session_id, line))
        hit_beat = (f"{actor['name'] if actor else '攻击者'} 用 {weapon} 命中 {victim['name']}，"
                    f"造成 {dmg['total']} 点伤害")
    else:
        out.append(_combat_line(db, session_id, "目标已不在场，伤害落空。"))

    state["pending_roll"] = None
    # 反击伤害（no_advance）时先攻已在 resolve_reaction 里推进过攻方回合，这里不再重复推进。
    if not pr.get("no_advance"):
        if actor:
            actor["acted_this_round"] = True
        engine.advance_turn(state)
    _save_combat(db, session_id, state)   # 先落库（清 pending_roll+推进），再驱动；
    # 绝不在 drive_npcs 之后再存 state——否则会把 drive 里 _end_combat 的「清空战斗」覆盖回去。
    drive_chunks, drive_beats = await drive_npcs(db, session_id, state, agent, scene_hint)
    beats = ([hit_beat] if hit_beat else []) + drive_beats
    narr: list[str] = []
    if agent and beats:
        prose = await agent.narrate(state, beats, scene_hint)
        if prose:
            narr.append(_combat_narration(db, session_id, prose))
    return narr + out + drive_chunks


def stage_aoe_damage(
    db: Session, session_id: str, thrower_id: str, target_names: list[str],
    weapon: str = "", formula: str = "", burning: bool = False, reason: str = "",
    dedup_key: str = "",
) -> tuple[str | None, bool]:
    """把一次范围/非常规攻击（燃烧弹/泼火/群体）的伤害挂成投掷者的 pending_roll——玩家亲手掷、
    一次结果应用到所有被波及的敌人（命中已由先前的投掷检定判定，不再逐个闪避）。

    伤害：能在武器表查到就用表值（燃烧瓶=2D6 烧，自动附燃烧）；查不到用 formula；都无回落 1D6。
    返回 (combat_state chunk, 是否挂上)。已在战斗中、无未决投掷、能解析到存活目标才挂。dedup_key
    与 state.last_aoe_key 相同则跳过（幂等，防重新生成重复挂）。
    """
    session = db.get(GameSession, session_id)
    state = get_combat(session)
    if not state or state.get("pending_roll"):
        return None, False
    if dedup_key and state.get("last_aoe_key") == dedup_key:
        return None, False
    victims = []
    for name in target_names:
        name = (name or "").strip()
        v = next((p for p in state.get("initiative") or []
                  if p["name"] == name or (name and name in p["name"])), None)
        if v and engine.is_active(v) and v.get("side") == "enemy" and v["id"] not in [x["id"] for x in victims]:
            victims.append(v)
    if not victims:
        return None, False

    w = engine.resolve_weapon(weapon) if weapon else None
    if w and w.get("name") not in ("徒手格斗", None):
        dmg = engine.roll_weapon_damage(w, "0")
        if any(k in (w.get("dam") or "") for k in ("烧", "燃烧")):
            burning = True
        weapon_name = w.get("name")
    else:
        dmg = engine.roll_weapon_damage({"dam": formula or "1D6"}, "0")
        weapon_name = weapon or "范围攻击"

    thrower = _find(state, thrower_id)
    state["pending_roll"] = {
        "id": uuid.uuid4().hex, "actor_id": thrower_id, "kind": "aoe_damage",
        "victim_ids": [v["id"] for v in victims], "weapon": weapon_name,
        "damage": {"total": dmg["total"], "rolls": list(dmg["rolls"]), "notation": dmg["notation"],
                   "flags": list(dmg.get("flags") or [])},
        "burning": burning,
        "reason": reason or f"{thrower['name'] if thrower else ''} 的 {weapon_name}",
        "label": f"投掷伤害（{dmg['notation']}）",
    }
    if dedup_key:
        state["last_aoe_key"] = dedup_key
    _save_combat(db, session_id, state)
    return _combat_state_chunk(state), True


async def _resolve_aoe_roll(
    db: Session, session_id: str, state: dict, actor: dict | None, pr: dict,
    agent=None, scene_hint: str = "",
) -> list[str]:
    """AoE 伤害第二段：玩家掷出的一份伤害应用到所有被波及敌人（+可选燃烧）。不推进先攻——
    它是投掷动作的后果、非独立回合；仅可能因致死而结束战斗。"""
    dmg = pr["damage"]
    weapon = pr.get("weapon") or "范围攻击"
    who = actor["name"] if actor else "攻击者"
    out = [_combat_roll_event(
        db, session_id, f"{who}（{weapon}）伤害 {dmg['notation']} = {dmg['total']}",
        _damage_dice_detail(dmg))]
    hit_names: list[str] = []
    for vid in pr.get("victim_ids") or []:
        victim = _find(state, vid)
        if victim and engine.is_active(victim):
            for line in apply_damage(db, state, victim, dmg["total"], reason=pr["reason"]):
                out.append(_combat_line(db, session_id, line))
            if pr.get("burning"):
                conds = victim.setdefault("conditions", [])
                if "burning" not in conds:
                    conds.append("burning")
            hit_names.append(victim["name"])
    state["pending_roll"] = None
    end = engine.check_combat_end(state.get("initiative") or [])
    if end:
        out += _end_combat(db, session_id, state, end)   # 内含清空战斗
        return out
    _save_combat(db, session_id, state)
    out.append(_combat_state_chunk(state))
    beat = (f"{who} 的 {weapon} 波及 {'、'.join(hit_names)}，各受 {dmg['total']} 点"
            + ("并起火燃烧" if pr.get("burning") else "")) if hit_names else f"{who} 的 {weapon} 落空"
    narr: list[str] = []
    if agent:
        prose = await agent.narrate(state, [beat], scene_hint)
        if prose:
            narr.append(_combat_narration(db, session_id, prose))
    return narr + out


async def resolve_reaction(db: Session, session_id: str, defender_id: str, choice: str,
                           agent=None, scene_hint: str = "") -> list[str]:
    """真人对一次针对自己的攻击做出反应（fight_back/dodge/cover），结算这一击后续跑驱动。

    P2 待接：fight_back 反击目前引擎硬编码徒手估伤（resolve_attack 的 _UNARMED + defender._db 恒 0），
    守方持械反击尚未接线；届时再给本函数与 resolve_attack 补 defender 武器入参。
    """
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
        # 攻方若已被缴械 → 强制徒手结算（与 _apply_one_action 的攻击分支一致）
        atk_disarmed = "disarmed" in (attacker.get("conditions") or [])
        res = engine.resolve_attack(
            _char_data(attacker), attacker.get("db", "0"), pr["weapon"],
            defender_data=_char_data(defender), defense=choice, ranged=pr["ranged"],
            attacker_disarmed=atk_disarmed,
        )
        # 玩家亲手做的这次反应检定 → 走 3D 骰动画 + 对抗卡（守方是玩家，动画呈现守方这一掷）
        react_content = _combat_dice_content(attacker, defender, pr["weapon"], res)
        out.append(_combat_roll_event(
            db, session_id, react_content,
            _hit_dice_detail(res.get("defender_check") or res["attacker_check"]),
            opposed=_opposed_detail(res, attacker["name"], defender["name"])))
        verb = {"fight_back": "反击", "dodge": "闪避", "cover": "扑向掩体"}.get(choice, choice)
        if res["hit"] and res["damage"] and res["damage_to"] == "attacker":
            # 守方（玩家）反击命中攻方 → 让玩家亲手掷反击伤害（两段式）：先清反应、推进攻方回合，
            # 再挂 pending_roll(no_advance)，由玩家经 /combat/roll 结算伤害。不在此自动扣血。
            state["pending_reaction"] = None
            attacker["acted_this_round"] = True
            engine.advance_turn(state)
            dmg = res["damage"]
            state["pending_roll"] = {
                "id": uuid.uuid4().hex, "actor_id": defender["id"], "kind": "damage",
                "victim_id": attacker["id"], "weapon": "反击",
                "damage": {"total": dmg["total"], "rolls": list(dmg["rolls"]),
                           "notation": dmg["notation"], "flags": list(dmg.get("flags") or [])},
                "no_advance": True, "reason": f"{defender['name']} 反击",
                "label": f"投掷反击伤害（{dmg['notation']}）",
            }
            state["log"].append({"round": state["round"], "actor": defender["name"],
                                 "action": "fight_back_hit", "target": attacker["name"]})
            _save_combat(db, session_id, state)
            out.append(_combat_state_chunk(state))   # 带 pending_roll → 前端弹「投掷反击伤害」
            return out
        if res["hit"] and res["damage"]:   # damage_to == 'defender'：守方被攻方命中 → NPC 伤害自动结算
            for line in apply_damage(db, state, defender, res["damage"]["total"],
                                     reason=f"{attacker['name']} 的 {pr['weapon']}"):
                out.append(_combat_line(db, session_id, line))
        beats.append(f"{defender['name']} 对 {attacker['name']} 的攻击选择{verb}："
                     + ("被击中" if (res["hit"] and res["damage_to"] == "defender") else "未受伤"))
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

    if atype == "extinguish":
        conds = actor.get("conditions") or []
        if "burning" in conds:
            conds.remove("burning")
            summary = f"{actor['name']} 扑打身上的火，暂时压住了火势"
        else:
            summary = f"{actor['name']} 身上并没有着火"
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "extinguish"})
        chunks.append(_combat_line(db, session_id, summary + "。"))
        return chunks, summary

    if atype == "first_aid":
        # 对己方受伤者施急救：目标存在、且该处伤尚未急救过（RAW 每处伤一次）
        target = _find(state, action.get("target_id"))
        if target is None:
            s = f"{actor['name']} 的急救目标已不在场"
            chunks.append(_combat_line(db, session_id, s + "。"))
            return chunks, s
        if target.get("first_aid_used"):
            s = f"{actor['name']} 想为 {target['name']} 急救，但这处伤已被处理过"
            chunks.append(_combat_line(db, session_id, s + "。"))
            return chunks, s
        r = engine.resolve_first_aid(_char_data(actor))
        for line in r["lines"]:
            chunks.append(_combat_line(db, session_id, f"{actor['name']}｜{line}"))
        heal_lines: list[str] = []
        if r["success"]:
            if target.get("status") == "dying":
                # 濒死稳住：dying → unconscious（稳定但出局），不回 HP
                target["status"] = "unconscious"
                line = f"{target['name']} 濒死伤势被稳住，转为昏迷（脱离濒死）。"
                chunks.append(_combat_line(db, session_id, line))
                heal_lines = [line]
                _sync_char_status(db, target)
            elif r["heal"]:
                heal_lines = apply_heal(db, state, target, r["heal"])
                for line in heal_lines:
                    chunks.append(_combat_line(db, session_id, line))
            target["first_aid_used"] = True
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "first_aid",
                             "target": target["name"], "success": r["success"]})
        summary = f"{actor['name']} 为 {target['name']} 急救：" + (
            "；".join(heal_lines) if heal_lines else ("成功" if r["success"] else "失败"))
        return chunks, summary

    if atype == "observe":
        r = engine.resolve_observe(_char_data(actor))
        for line in r["lines"]:
            chunks.append(_combat_line(db, session_id, f"{actor['name']}｜{line}"))
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "observe",
                             "success": r["success"]})
        summary = f"{actor['name']} 观察战场：" + ("察觉到敌方破绽/动向" if r["success"] else "未看出更多")
        return chunks, summary

    if atype == "maneuver":
        target = _find(state, action.get("target_id"))
        if target is None or not engine.is_active(target):
            s = f"{actor['name']} 的机动目标已不在场"
            chunks.append(_combat_line(db, session_id, s + "。"))
            return chunks, s
        kind = action.get("kind") or "grapple"
        r = engine.resolve_maneuver(_char_data(actor), _char_data(target), kind=kind)
        for line in r["lines"]:
            chunks.append(_combat_line(db, session_id, f"{actor['name']} → {target['name']}｜{line}"))
        if r["success"] and r["condition"]:
            conds = target.setdefault("conditions", [])
            if r["condition"] not in conds:   # 去重
                conds.append(r["condition"])
        kind_cn = "擒抱" if kind == "grapple" else "缴械"
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "maneuver",
                             "target": target["name"], "kind": kind, "success": r["success"]})
        summary = f"{actor['name']} 对 {target['name']} 尝试{kind_cn}：" + ("得手" if r["success"] else "未得手")
        return chunks, summary

    if atype == "reload":
        actor["loaded"] = True
        summary = f"{actor['name']} 装填弹药，武器就绪"
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "reload"})
        chunks.append(_combat_line(db, session_id, summary + "。"))
        return chunks, summary

    if atype == "aim":
        actor["aim"] = True
        summary = f"{actor['name']} 举枪瞄准，下一击更准"
        state["log"].append({"round": state["round"], "actor": actor["name"], "action": "aim"})
        chunks.append(_combat_line(db, session_id, summary + "。"))
        return chunks, summary

    # attack
    target = _find(state, action.get("target_id"))
    if target is None or not engine.is_active(target):
        s = f"{actor['name']} 的目标已不在场"
        chunks.append(_combat_line(db, session_id, s + "。"))
        return chunks, s
    weapon = action.get("weapon") or actor.get("weapon") or "徒手格斗"
    # 缴械：强制徒手（连带把武器/远程判定改成徒手），瞄准：命中检定加 1 奖励骰后清标记
    disarmed = "disarmed" in (actor.get("conditions") or [])
    if disarmed:
        weapon = "徒手格斗"
    ranged = _weapon_is_firearm(weapon)
    aim_bonus = 1 if actor.get("aim") else 0
    # NPC 防御者被擒抱 → 近战无法闪避，收窄到反击（与真人 pending_reaction.allowed 对称）
    target_grappled = "grappled" in (target.get("conditions") or [])
    defense = None if ranged else (
        action.get("defense")
        or engine.heuristic_defense(target, is_firearm=False, defender_grappled=target_grappled))
    res = engine.resolve_attack(
        _char_data(actor), actor.get("db", "0"), weapon,
        defender_data=_char_data(target), defense=defense, ranged=ranged,
        attacker_disarmed=disarmed, bonus=aim_bonus,
    )
    if actor.get("aim"):
        actor["aim"] = False   # 瞄准一次性消费（aim 只在此攻击分支清；observe/maneuver 不碰它，
        # 故瞄准会一直保持到下一次攻击——「瞄准保持到下一击」的有意设计，勿改）
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
        # 燃烧 tick：着火者回合开始先烧一次（1D6），可能致其濒死/死亡 → 须在 is_active 判断前。
        if "burning" in (actor.get("conditions") or []) and engine.is_active(actor):
            burn = engine.roll_weapon_damage({"dam": "1D6"}, "0")["total"]
            for line in apply_damage(db, state, actor, burn, reason="持续燃烧", ignore_armor=True):
                chunks.append(_combat_line(db, session_id, line))
            _save_combat(db, session_id, state)
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
                # 名字一并落进 pending_reaction：广播与断线重连（_combat_meta 透传）都要用它渲染
                # 提示文案，否则重连回来缺 *_name 会显示「undefined 用 X 攻击你」。
                state["pending_reaction"] = {
                    "attacker_id": actor["id"], "defender_id": target["id"],
                    "attacker_name": actor["name"], "defender_name": target["name"],
                    "weapon": weapon, "ranged": is_fire,
                    "allowed": engine.allowed_reactions(
                        is_fire, defender_grappled="grappled" in (target.get("conditions") or [])),
                }
                _save_combat(db, session_id, state)
                chunks.append(_chunk("combat_reaction_prompt", metadata=state["pending_reaction"]))
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
    # combat_log 标记：前端据此把机械结算行归入折叠战斗日志抽屉，不灌主聊天流（KP 叙述不打此标记）。
    ev = session_service.add_event(db, session_id, "system", text, actor_name="战斗")
    return _chunk("system", text, id=ev.id, metadata={"combat_log": True})


def _combat_narration(db: Session, session_id: str, text: str) -> str:
    """战斗子代理的一段叙述：落成 narration 事件（进历史/复盘），返回广播 chunk。"""
    ev = session_service.add_event(db, session_id, "narration", text, actor_name="KP")
    return _chunk("narration_full", text, id=ev.id, actor_name="KP")


def _combat_dice_content(actor: dict, target: dict, weapon: str, res: dict) -> str:
    """命中检定的可读描述（供骰事件/日志）：含攻守检定与命中与否。"""
    atk = res["attacker_check"]
    dfn = res.get("defender_check")
    if dfn is not None:
        return (f"{actor['name']}（{weapon}）{atk.description} vs {target['name']} {dfn.description}"
                f" → {'命中' if res['hit'] else '未命中'}")
    return f"{actor['name']}（{weapon}）{atk.description} → {'命中' if res['hit'] else '未命中'}"


def _combat_dice(db: Session, session_id: str, actor: dict, target: dict, weapon: str, res: dict) -> str:
    content = _combat_dice_content(actor, target, weapon, res)
    ev = session_service.add_event(db, session_id, "dice", content, actor_name="战斗",
                                   metadata={"combat_attack": True, "hit": res["hit"]})
    return _chunk("dice", content, id=ev.id, metadata={"combat_log": True, "combat_attack": True, "hit": res["hit"]})


def _hit_dice_detail(chk) -> dict:
    """命中检定（d100）→ 前端 3D 骰契约（kind=check），与主线技能检定同一口径。"""
    return {"kind": "check", "result": chk.roll, "tens": list(chk.tens), "tens_kept": chk.tens_kept,
            "units": chk.units, "bonus": chk.bonus, "penalty": chk.penalty}


def _check_side(chk, name: str) -> dict:
    """一方的检定结果投影（供前端对抗卡并排渲染）。"""
    return {"name": name, "roll": chk.roll, "target": chk.skill_value,
            "skill": chk.skill_name, "outcome": chk.outcome}


def _CHK_OK(chk) -> bool:
    """检定是否达成（大成功/困难成功/普通成功都算命中该检定）。"""
    return chk is not None and chk.outcome in ("critical_success", "hard_success", "success")


def _opposed_detail(res: dict, actor_name: str, target_name: str) -> dict:
    """攻守两方检定的结构化对抗数据（前端据此画「两边并排 + VS + 高亮胜方」的对抗卡）。

    winner/result 语义：
    - 命中 → 攻方胜；反击得手 → 守方胜（守方反击命中攻方）；
    - 被闪开/防住 → 守方胜（守方闪避/招架**检定成功**、主动化解了攻击）；
    - 未命中 → 无胜方（攻方自己失手，守方并未出力防住，不能算守方赢）。
    远程无守方检定（defender=None）时只呈现攻方一侧。
    """
    atk = res["attacker_check"]
    dfn = res.get("defender_check")
    d: dict = {"attacker": _check_side(atk, actor_name),
               "defender": _check_side(dfn, target_name) if dfn is not None else None}
    if res["hit"]:
        if res.get("damage_to") == "defender":
            d["winner"], d["result"] = "attacker", "命中"
        else:
            d["winner"], d["result"] = "defender", "反击得手"
    elif _CHK_OK(dfn):
        d["winner"], d["result"] = "defender", "被闪开/防住"   # 守方检定成功、真的防住了
    else:
        d["winner"], d["result"] = None, "未命中"              # 攻方失手，无人取胜
    return d


def _damage_dice_detail(dmg: dict) -> dict:
    """武器伤害（骰池）→ 前端 3D 骰契约（kind=pool）。sides 取骰式首段（多骰种时近似）。"""
    notation = dmg.get("notation") or ""
    m = re.match(r"\s*\d+[dD](\d+)", notation)
    sides = int(m.group(1)) if m else 6
    rolls = list(dmg.get("rolls") or [])
    total = int(dmg.get("total") or 0)
    return {"kind": "pool", "notation": notation,
            "dice": [{"sides": sides, "value": v} for v in rolls],
            "modifier": total - sum(rolls), "total": total,
            "flags": list(dmg.get("flags") or [])}   # 贯穿/燃烧/晕 → 前端伤害卡标注


def _combat_roll_event(db: Session, session_id: str, content: str, dice_detail: dict,
                       opposed: dict | None = None) -> str:
    """玩家亲自掷的战斗骰（命中/伤害）：落成 dice 事件并带 metadata.dice → 走主线 3D 骰动画。

    不打 combat_log 标记（那会被前端分流进日志抽屉、不触发动画）；combat_roll 仅作信息标注。
    opposed 非空时附上攻守两方检定投影 → 前端画「两边并排 + VS + 高亮胜方」的对抗卡。
    **即时广播**：点掷骰后随即让 3D 动画起来，不必等后续 NPC 驱动/子代理叙述（LLM）跑完——
    那些延迟以前会卡在「点了按钮到骰子动」之间。端点随整批返回时会再广播一次，前端按 id 幂等去重。
    """
    meta: dict = {"dice": dice_detail, "combat_roll": True}
    if opposed:
        meta["opposed"] = opposed
    ev = session_service.add_event(db, session_id, "dice", content, actor_name="战斗", metadata=meta)
    chunk = _chunk("dice", content, id=ev.id, metadata=meta)
    room_hub.broadcast(session_id, chunk)
    return chunk


def _combat_state_chunk(state: dict) -> str:
    """把当前战斗态（回合/先攻序/各方HP/轮到谁）广播给前端渲染战斗视图。"""
    return _chunk("combat_state", metadata=_combat_meta(state))
