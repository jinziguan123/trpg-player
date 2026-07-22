"""回合中的确定性角色、检定、场景和手书副作用。"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid

from sqlalchemy.orm import Session

from app.ai.llm_factory import get_llm
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import (
    dice_runtime,
    human_kp_service,
    illustration_service,
    session_service,
    turn_context,
    world_memory,
)
from app.services.event_protocol import make_chunk as _make_chunk

logger = logging.getLogger(__name__)

ALWAYS_BLIND_SKILLS = dice_runtime.ALWAYS_BLIND_SKILLS
TIER_LABEL = dice_runtime.TIER_LABEL
_check_prompt_text = dice_runtime._check_prompt_text
_resolve_check_actor = dice_runtime._resolve_check_actor
_parse_bonus_penalty = dice_runtime._parse_bonus_penalty
_check_dice_detail = dice_runtime._check_dice_detail
_pool_dice_detail = dice_runtime._pool_dice_detail
_scene_requires_group_check = dice_runtime._scene_requires_group_check
_resolve_san_targets = dice_runtime._resolve_san_targets
_resolve_dice_group_targets = dice_runtime._resolve_dice_group_targets
_ALL_TOKENS = dice_runtime._ALL_TOKENS
_resolve_scene_ref = turn_context._resolve_scene_ref
_scene_name = turn_context._scene_name
_apply_world_memory = turn_context._apply_world_memory
_match_single_npc = turn_context._match_single_npc
_maybe_scene_illustration = illustration_service._maybe_scene_illustration
_illustrate_handout = illustration_service._illustrate_handout


def _update_character_stat(db: Session, char: Character, path: str, value) -> None:
    """更新角色 system_data 中的嵌套字段并持久化"""
    sd = dict(char.system_data or {})
    parts = path.split(".")
    target = sd
    for p in parts[:-1]:
        if p not in target or not isinstance(target[p], dict):
            target[p] = {}
        target[p] = dict(target[p])
        target = target[p]
    target[parts[-1]] = value
    char.system_data = sd
    db.add(char)
    db.commit()
    db.refresh(char)


# 疯狂/失能状态严重度：疯狂不覆盖更严重的既有状态，永久疯狂也不被临时疯狂降级。
_STATUS_SEVERITY = {
    "active": 0, "major_wound": 1, "temporary_insanity": 2,
    "indefinite_insanity": 3, "unconscious": 4, "permanent_insanity": 5, "dead": 6,
}


def _apply_madness_status(
    db: Session, char: Character, new_san: int, went_insane: bool,
) -> str | None:
    """据 SAN 结果落疯狂状态（确定性）：SAN 归零→永久疯狂；一次损失≥当前SAN/5→临时疯狂。

    只在新状态比既有更严重时升级（不把昏迷/死亡/永久疯狂降级为临时疯狂）。返回落定的状态或 None。
    """
    if new_san <= 0:
        target = "permanent_insanity"
    elif went_insane:
        target = "temporary_insanity"
    else:
        return None
    cur = char.status or "active"
    if _STATUS_SEVERITY.get(target, 0) <= _STATUS_SEVERITY.get(cur, 0):
        return None  # 既有状态已同等或更严重，不降级
    char.status = target
    if target == "temporary_insanity":
        # 一阵疯狂：掷 1D10 随机症状 + 1D10 回合时长，存进角色卡（影响检定与言行，到期自动解除）。
        from app.rules.coc import madness as coc_madness
        sd = dict(char.system_data or {})
        sd["madness"] = coc_madness.make_bout()
        char.system_data = sd
    db.add(char)
    db.commit()
    return target


def _tick_madness_recovery(
    db: Session, session_id: str, chars: list[Character | None],
) -> list[str]:
    """每个玩家行动回合开始时，给在场角色进行中的『一阵疯狂』各减 1 回合；减到 0 → 解除
    （status 回 active、清 system_data.madness），广播恢复消息 + 角色卡刷新。返回待广播 chunks。"""
    out: list[str] = []
    for char in chars:
        if char is None or char.status != "temporary_insanity":
            continue
        sd = dict(char.system_data or {})
        bout = sd.get("madness")
        if not bout:
            continue
        left = int(bout.get("turns_left") or 0) - 1
        if left > 0:
            bout = dict(bout); bout["turns_left"] = left
            sd["madness"] = bout
            char.system_data = sd
            db.add(char); db.commit()
            continue
        sd.pop("madness", None)                 # 到期解除
        char.system_data = sd
        char.status = "active"
        db.add(char); db.commit()
        line = f"{char.name} 从『{bout.get('label')}』的疯狂发作中缓过神来，重新恢复了神智。"
        ev = session_service.add_event(
            db, session_id, "system", line, actor_name="系统",
            metadata={"madness_recovered": True, "actor": char.name},
        )
        out.append(_make_chunk("system", line, event_id=ev.id))
        out.append(_make_chunk("character_update", metadata={"char_id": char.id}))
    return out


# ── 指令执行器（旧正则路径与 agent loop 共用的单一实现）─────────────────────
# 每个函数执行一条指令并返回「待广播的 chunks（事件已落库）」+ 路径各自需要的信息。
# 旧路径（_process_commands）按正则匹配后调用；loop 路径（_build_kp_tool_executor）
# 按工具调用分发——执行逻辑只此一份，不复制。


async def _exec_san_check(
    db: Session, session_id: str, game_session: GameSession, kv: dict,
    player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], list[str]]:
    """执行一条理智检定：目睹者各自结算（同一角色对同一恐怖源只检定一次）。

    返回 (chunks, 回灌 KP 的结果描述列表)。
    """
    from app.rules.coc.checks import san_check

    chunks: list[str] = []
    descs: list[str] = []
    success_loss = (kv.get("success_loss") or "0").strip()
    failure_loss = (kv.get("failure_loss") or "1d6").strip()
    source = (kv.get("source") or "").strip()
    targets = _resolve_san_targets(kv.get("chars"), player_char, teammates)

    # 同一角色对同一恐怖源只检定一次：用 world_state.san_checked 记 "source|char_id"。
    ws = dict(game_session.world_state or {})
    san_checked = set(ws.get("san_checked") or [])
    san_dirty = False

    for tchar in targets:
        key = f"{source}|{tchar.id}" if source else None
        if key and key in san_checked:
            continue  # 该角色已对此恐怖源检定过，不重复

        char_data = {
            "base_attributes": tchar.base_attributes,
            "skills": tchar.skills,
            "system_data": tchar.system_data,
        }
        result = san_check(char_data, success_loss, failure_loss)
        check = result["check"]
        _update_character_stat(db, tchar, "sanity.current", result["new_san"])

        outcome_text = "成功" if check.outcome in (
            "critical_success", "hard_success", "success") else "失败"
        dice_content = (
            f"{tchar.name}｜理智检定：{check.description}\n"
            f"SAN 损失：{result['san_loss']}（{result['old_san']} → {result['new_san']}）"
        )
        # 疯狂状态落库（确定性，不依赖 KP 自觉）：SAN 归零→永久疯狂；一次损失≥当前SAN/5→临时疯狂。
        # 不覆盖更严重的既有状态（死亡/昏迷/永久疯狂不被降级）。
        madness = _apply_madness_status(db, tchar, result["new_san"], result["went_insane"])
        if madness == "permanent_insanity":
            dice_content += "\n永久疯狂！SAN 归零，调查员就此永远失常。"
        elif madness == "temporary_insanity":
            bout = (tchar.system_data or {}).get("madness") or {}
            sym = f"：【{bout.get('label')}】{bout.get('manifest')}" if bout else ""
            dice_content += f"\n临时疯狂发作{sym}（约 {bout.get('turns_left', '?')} 回合内影响其检定与言行）"

        dice_meta = {
            "skill": "SAN",
            "actor": tchar.name,
            "skill_value": result["old_san"],
            "roll": check.roll,
            "target": check.target,
            "outcome": outcome_text,
            "san_loss": result["san_loss"],
            "new_san": result["new_san"],
            "went_insane": result["went_insane"],
        }
        # SAN 检定明骰：先落 SAN 判定本身的 d100 明细（check），再落损失骰池（pool）。
        # 前端据 dice 播 SAN 判定动画，据 loss_dice 播损失骰动画。
        dice_meta["check_dice"] = _check_dice_detail(check)
        loss_roll = result.get("loss_roll")
        if loss_roll is not None:
            dice_meta["dice"] = _pool_dice_detail(loss_roll)
        else:
            # 固定损失（如成功 0）：无骰池，明细直给定值，前端不必播骰。
            dice_meta["dice"] = {
                "kind": "pool", "notation": "0", "dice": [],
                "modifier": result["san_loss"], "total": result["san_loss"],
            }
        ev = session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        chunks.append(_make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id))
        # 通知前端刷新角色卡（SAN/疯狂状态已变）——与 inventory_update 同一套刷新机制。
        chunks.append(_make_chunk("character_update", metadata={"char_id": tchar.id}))
        descs.append(
            f"{tchar.name} 理智检定（{outcome_text}）：损失 {result['san_loss']} SAN"
            f"（{result['old_san']}→{result['new_san']}）"
        )
        if key:
            san_checked.add(key)
            san_dirty = True

    if san_dirty:
        ws["san_checked"] = sorted(san_checked)
        game_session.world_state = ws
        db.add(game_session)
        db.commit()
    return chunks, descs


def _resolve_hp_target(
    target_str: str, player_char: Character, teammates: list[Character] | None,
) -> Character | None:
    """把 HP_CHANGE 的 target= 解析成玩家方角色：空/player/主角/主角名→主角；队友名→该队友。

    NPC 或匹配不到 → None（NPC 的 HP 本系统不逐一追踪，维持不结算）。
    """
    name = (target_str or "").strip()
    if not name or name.lower() == "player" or name in ("主角", "玩家", player_char.name):
        return player_char
    for t in (teammates or []):
        if t.name and (t.name == name or name in t.name or t.name in name):
            return t
    return None


# ── 规则固定的治疗效果确定性结算（不依赖 KP 自觉发 HP_CHANGE）──
_HEAL_SUCCESS_OUTCOMES = ("critical_success", "hard_success", "success")


def _heal_kind(skill: str) -> str | None:
    """技能名 → 治疗类型：急救 / 医学 / None（非治疗技能）。"""
    s = skill or ""
    if "急救" in s:
        return "first_aid"
    if "医学" in s:
        return "medicine"
    return None


def _infer_heal_target(
    medic_id: str | None, player_char: Character, teammates: list[Character] | None,
) -> Character | None:
    """治疗检定没显式给 target 时推断被治疗者：排除施救者后，优先唯一的濒死者，否则唯一的受伤者。

    多人濒死/多人受伤（且无唯一濒死）时不猜——宁可不治也不治错人（要求 KP 明确 target）。
    这样急救不再依赖 KP 记得写 target：施救对象通常就是那个倒下/流血的队友。
    """
    def _hp(c: Character) -> tuple[int, int]:
        hp = (c.system_data or {}).get("hitPoints") or {}
        return int(hp.get("current") or 0), int(hp.get("max") or 0)

    cands = [c for c in ([player_char] + list(teammates or [])) if c and c.id != medic_id]
    dying = [c for c in cands if _hp(c)[0] <= 0 or c.status in ("dying", "unconscious")]
    if dying:
        return dying[0] if len(dying) == 1 else None
    wounded = [c for c in cands if _hp(c)[0] < _hp(c)[1]]
    return wounded[0] if len(wounded) == 1 else None


def _apply_heal_on_success(
    db: Session, session_id: str, target: Character | None, skill: str, outcome: str,
) -> list[str]:
    """急救/医学检定成功 → 引擎确定性给目标回血（规则固定效果，KP 不必也不该再发 HP_CHANGE）。

    急救：回 1 点，濒死（HP≤0/昏迷）则稳住并唤醒；医学：回 1D3。每处伤只成功处理一次
    （system_data.firstAidUsed，受新伤时由 _exec_hp_change 清零）。失败/非治疗技能/无目标不结算。
    返回可读结算 chunks（system 事件，已落库）。
    """
    kind = _heal_kind(skill)
    if kind is None or outcome not in _HEAL_SUCCESS_OUTCOMES or target is None:
        return []
    sd = dict(target.system_data or {})
    if sd.get("firstAidUsed"):
        line = f"{target.name} 这处伤已被成功处理过（每处伤急救/医学各只能成功一次），本次不叠加。"
        ev = session_service.add_event(db, session_id, "system", line, actor_name="系统",
                                       metadata={"heal": 0, "actor": target.name})
        return [_make_chunk("system", line, event_id=ev.id)]
    hp = dict(sd.get("hitPoints") or {})
    old = int(hp.get("current") or 0)
    max_hp = int(hp.get("max") or old or 1)
    dying = old <= 0 or target.status in ("dying", "unconscious")
    heal = 1 if kind == "first_aid" else random.randint(1, 3)
    new_hp = min(max_hp, max(old, 0) + heal)
    if dying:
        new_hp = max(new_hp, 1)   # 濒死稳住：至少留 1 点临时生命

    _update_character_stat(db, target, "hitPoints.current", new_hp)
    sd = dict(target.system_data or {})
    sd["firstAidUsed"] = True          # once-per-wound：直到受新伤才清
    target.system_data = sd
    if dying and new_hp > 0 and target.status in ("dying", "unconscious"):
        target.status = "active"        # 成功急救稳住濒死 / 唤醒昏迷
    db.add(target)
    db.commit()

    label = "急救" if kind == "first_aid" else "医学"
    gained = new_hp - max(old, 0)
    line = f"{target.name} 经{label}恢复 {gained} 点生命（HP {old} → {new_hp}）"
    if dying:
        line += "，伤势稳住、脱离濒死"
    ev = session_service.add_event(db, session_id, "system", line, actor_name="系统",
                                   metadata={"heal": gained, "old_hp": old, "new_hp": new_hp,
                                             "actor": target.name})
    return [
        _make_chunk("system", line, event_id=ev.id),
        _make_chunk("character_update", metadata={"char_id": target.id}),  # 刷新角色卡 HP
    ]


async def _exec_hp_change(
    db: Session, session_id: str, player_char: Character,
    target_str: str, delta_str: str, reason: str,
    module: Module | None = None,
    teammates: list[Character] | None = None,
) -> list[str]:
    """执行一条 HP 变化结算（target=player/主角 或队友名；NPC/匹配不到则不结算）。返回 chunks。

    确定性规则钩子（CoC 7e，不依赖 KP 自觉）：单次伤害 ≥ 最大 HP 一半即重伤——
    落 major_wound 状态并**系统自动**过一次体质（CON）检定，失败则昏迷（unconscious）。
    昏迷判定是被动生理反应而非玩家主动行动，故不走「待玩家投骰」、直接自动掷。
    队友受伤同样结算（多人局队友也会重伤/昏迷）。
    """
    char = _resolve_hp_target(target_str, player_char, teammates)
    if char is None:
        return []
    try:
        delta = int(str(delta_str).strip())
    except ValueError:
        return []
    reason = (reason or "").strip()
    hp_data = char.system_data.get("hitPoints", {})
    old_hp = hp_data.get("current", 0)
    max_hp = hp_data.get("max", old_hp)
    new_hp = max(0, min(max_hp, old_hp + delta))

    _update_character_stat(db, char, "hitPoints.current", new_hp)
    if delta < 0 and (char.system_data or {}).get("firstAidUsed"):
        # 受新伤 = 新的急救机会：清 once-per-wound 标记，否则之前被急救过的人再受伤后救不了。
        sd = dict(char.system_data or {}); sd["firstAidUsed"] = False
        char.system_data = sd; db.add(char); db.commit()

    chunks: list[str] = []
    if delta < 0:
        dmg = abs(delta)
        hp_content = f"{char.name} 受到 {dmg} 点伤害（HP {old_hp} → {new_hp}）"
        if reason:
            hp_content += f"——{reason}"
        major_wound = dmg >= max_hp // 2 and max_hp > 0
        already_major = char.status == "major_wound"
        if max_hp > 0 and dmg > max_hp:
            # 单次伤害 > 最大 HP → 当场毙命（CoC 7e）
            char.status = "dead"; db.add(char); db.commit()
            hp_content += "\n单次伤害超过最大生命值，当场毙命！"
        elif new_hp <= 0:
            # 归零：受过重伤（本击重伤 或 已带重伤标记）→ 濒死；只受轻伤 → 昏迷（稳定、不致死）
            if major_wound or already_major:
                char.status = "dying"
                hp_content += "\n濒死！需急救稳住，否则每轮体质检定失败即死。"
            else:
                char.status = "unconscious"
                hp_content += "\n昏迷倒地（生命值归零，但只受轻伤、伤势稳定、不致死）。"
            db.add(char); db.commit()
            major_wound = False   # 已按归零结算，不再走下方「未归零重伤体质检定」
        elif major_wound:
            hp_content += "\n重伤！"
    else:
        hp_content = f"{char.name} 恢复 {delta} 点生命（HP {old_hp} → {new_hp}）"
        if reason:
            hp_content += f"——{reason}"
        major_wound = False

    ev = session_service.add_event(
        db, session_id, "system", hp_content,
        actor_name="系统",
        metadata={"hp_change": delta, "old_hp": old_hp, "new_hp": new_hp, "actor": char.name},
    )
    chunks.append(_make_chunk("system", hp_content, event_id=ev.id))

    # 重伤（未至濒死）→ 状态落库 + 自动体质检定判昏迷。fail-open：检定异常不阻塞结算。
    if major_wound and new_hp > 0 and module is not None:
        try:
            char.status = "major_wound"
            db.add(char)
            db.commit()
            engine = get_engine(module.rule_system)
            cdata = {
                "base_attributes": char.base_attributes,
                "skills": char.skills,
                "system_data": char.system_data,
            }
            result = engine.resolve_check(cdata, "体质", "normal")
            con_content = (
                f"{char.name}｜重伤体质检定（判定是否昏迷）：{result.description}"
            )
            if result.outcome in ("failure", "fumble"):
                char.status = "unconscious"
                db.add(char)
                db.commit()
                con_content += f"\n{char.name} 眼前一黑，昏迷倒地！"
            dev = session_service.add_event(
                db, session_id, "dice", con_content,
                actor_name="系统", metadata={
                    "skill": "体质", "roll": result.roll, "target": result.target,
                    "outcome": result.outcome, "actor": char.name,
                    "major_wound_check": True, "dice": _check_dice_detail(result),
                },
            )
            chunks.append(_make_chunk("dice", con_content, event_id=dev.id))
        except Exception:
            logger.exception("重伤体质检定失败（忽略，不阻塞结算）: char=%s", char.id)
    # 通知前端刷新角色卡（HP/状态已变）——与 inventory_update 同一套刷新机制。
    chunks.append(_make_chunk("character_update", metadata={"char_id": char.id}))
    return chunks


async def _exec_dice_check(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    kv: dict, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], list[str], bool]:
    """执行一条技能检定。返回 (chunks, 回灌 KP 的结果描述, 是否挂成「待玩家投骰」)。

    真人控制、非暗投 → 不自动掷，挂 pending 并广播检定提示（pending=True，本轮就此收束）；
    NPC 暗骰 / AI 队友 / 暗投 → 系统自动掷，结果回灌。
    """
    chunks: list[str] = []
    descs: list[str] = []
    skill_name = (kv.get("skill") or "").strip()
    if not skill_name:
        return chunks, descs, False
    difficulty = (kv.get("difficulty") or "normal").strip() or "normal"
    char_ref = (kv.get("char") or "").strip()
    # 兼容旧模组：planner/旧 KP 没有携带 chars 时，只有明确唯一的场景群检机制点才补全。
    # 显式 char/chars 永远优先，多个同技能机制点则不猜。
    if not char_ref and not (kv.get("chars") or "").strip():
        if _scene_requires_group_check(module, game_session, player_char, skill_name):
            char_ref = "在场"
    blind = (kv.get("visibility") or "open").strip().lower() == "blind"
    # 心理学等技能一律强制暗投：即使 KP 写了 visibility=open 或没写，也不挂「待玩家投骰」、
    # 不广播达成等级——结果只回灌 KP，玩家永远看不到成败。
    if any(s in skill_name for s in ALWAYS_BLIND_SKILLS):
        blind = True
    source = (kv.get("source") or "").strip()
    bonus, penalty = _parse_bonus_penalty(kv)

    # 群检：公共/被动感知事件（一声响、一个可触发灵感的线索——在场人人都可能注意到），
    # char=在场/全体 或 chars=<名单> → 在场每个玩家角色各自检定。被动性质天然自动掷，
    # 不逐人挂「待玩家投骰」（否则每有环境声响就要每个真人各点一次投骰，极其累赘）。
    group_ref = (kv.get("chars") or "").strip()
    if char_ref in _ALL_TOKENS or group_ref:
        targets = _resolve_dice_group_targets(
            char_ref, group_ref, game_session, player_char, teammates,
        )
        for c in targets:
            cdata = {
                "base_attributes": c.base_attributes,
                "skills": c.skills,
                "system_data": c.system_data,
            }
            rc, rd = await _auto_roll_check(
                db, session_id, game_session, module, cdata, c.name, False,
                skill_name, difficulty, blind, source, bonus, penalty,
            )
            chunks += rc
            descs += rd
        return chunks, descs, False

    char_data, disp_name, is_npc, char_id = _resolve_check_actor(
        char_ref, skill_name, player_char, teammates, module,
    )

    # req 1/2：真人控制、且非暗投的检定 → 不自动掷，挂成「待玩家投骰」并给出提示；
    # NPC 暗骰 / AI 队友 / 暗投 仍由系统自动掷（无人点投骰，避免卡住）。
    if (
        not is_npc and not blind
        and session_service.is_human_controlled(db, session_id, char_id)
    ):
        # 去重：分头行动下同一 plan 被注入每个分组，多组常各自吐出同一条 [DICE_CHECK]，
        # 合并文本后逐条处理会重复挂 pending、弹出两张相同的投骰卡。已存在等价（同角色+技能+
        # 难度）待投检定则跳过——不重复挂、不再广播 check_request（仍返回 True 收束本轮）。
        if session_service.find_pending_check(db, session_id, char_id, skill_name, difficulty):
            return chunks, descs, True
        check_id = uuid.uuid4().hex
        pending = {
            "id": check_id, "skill": skill_name, "difficulty": difficulty,
            "char_ref": char_ref, "char_id": char_id, "actor_name": disp_name,
            "source": source, "bonus": bonus, "penalty": penalty,
        }
        # 治疗类检定：确定被治疗者（KP 显式 target 优先；缺失/误写成施救者自己 → 推断濒死/受伤队友），
        # 投骰成功后由系统确定性回血——不再依赖 KP 记得写 target。
        if _heal_kind(skill_name):
            tgt_str = (kv.get("target") or "").strip()
            heal_target = _resolve_hp_target(tgt_str, player_char, teammates) if tgt_str else None
            if heal_target is None or heal_target.id == char_id:
                heal_target = _infer_heal_target(char_id, player_char, teammates) or heal_target
            if heal_target is not None:
                pending["heal_target_id"] = heal_target.id
        session_service.add_pending_check(db, session_id, pending)
        prompt_text = _check_prompt_text(disp_name, skill_name, difficulty)
        meta = {"check_request": True, **pending}
        ev = session_service.add_event(
            db, session_id, "system", prompt_text, actor_name="系统", metadata=meta,
        )
        chunks.append(_make_chunk(
            "check_request", prompt_text, metadata=meta,
            event_id=ev.id, actor_id=char_id,
        ))
        return chunks, descs, True  # 等玩家 /roll，本轮不掷、不续写

    rc, rd = await _auto_roll_check(
        db, session_id, game_session, module, char_data, disp_name, is_npc,
        skill_name, difficulty, blind, source, bonus, penalty,
    )
    return chunks + rc, descs + rd, False


async def _auto_roll_check(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    char_data: dict, disp_name: str, is_npc: bool,
    skill_name: str, difficulty: str, blind: bool, source: str,
    bonus: int, penalty: int,
) -> tuple[list[str], list[str]]:
    """系统自动掷一次检定并落库（不挂 pending）。单人自动路径与群检各成员共用。

    返回 (chunks, 回灌 KP 的结果描述)。暗投不落 dice 明细（会反推成败）。
    """
    chunks: list[str] = []
    descs: list[str] = []
    engine = get_engine(module.rule_system)
    result = engine.resolve_check(char_data, skill_name, difficulty, bonus=bonus, penalty=penalty)
    tier_cn = TIER_LABEL.get(result.tier, result.tier)

    if blind:
        kind_word = "暗骰" if is_npc else "暗投"
        dice_content = f"{disp_name} 进行了一次{kind_word}·{skill_name}（结果仅 KP 可见）"
        dice_meta = {"skill": skill_name, "actor": disp_name, "blind": True}
        descs.append(
            f"【{kind_word}·{disp_name}·{skill_name}（{difficulty}），结果仅你（KP）可见，"
            f"绝不可直接把成败告诉玩家】：达成 {tier_cn}；{result.description}"
        )
    else:
        dice_content = (
            f"{disp_name}｜{skill_name} 检定（{difficulty}）：{tier_cn}（{result.description}）"
        )
        dice_meta = {
            "skill": skill_name,
            "skill_value": result.skill_value,
            "roll": result.roll,
            "target": result.target,
            "outcome": result.outcome,
            "tier": result.tier,
            "actor": disp_name,
            "dice": _check_dice_detail(result),
        }
        descs.append(
            f"{disp_name} {skill_name}（{difficulty}），达成 {tier_cn}"
            + (f"（针对：{source}）" if source else "")
            + f"：{result.description}"
        )

    ev = session_service.add_event(
        db, session_id, "dice", dice_content,
        actor_name="系统", metadata=dice_meta,
    )
    chunks.append(_make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id))

    # 世界记忆钩子 d：暗投（玩家/队友对 NPC 的心理学等）若能经 source= 确定性归属到
    # 唯一 NPC，记录其「被看穿/未被看穿」；NPC 自己的暗骰或归属不成立则跳过。
    if blind and not is_npc:
        target = _match_single_npc(module, source)
        if target:
            seen_through = result.outcome in (
                "critical_success", "hard_success", "success",
            )
            verdict = "看穿" if seen_through else "试探，但未被看穿"
            _apply_world_memory(
                db, game_session,
                lambda ws: world_memory.record_npc_interaction(
                    ws, target[0], ev.sequence_num,
                    f"被 {disp_name} 用{skill_name}{verdict}",
                ),
            )
    return chunks, descs


async def _exec_scene_change(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    ref: str, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], str | None, str]:
    """执行一次场景切换。返回 (chunks, 发生切换的 scene_id 或 None, 未切换的原因说明)。

    确定性连通校验：目标须沿 connections 连通图从当前场景可达（模组没建图时不启用，
    行为与从前一致）——KP/planner 说「玩家到了X」也搬不动不连通的场景，杜绝叙事瞬移。
    """
    sid = _resolve_scene_ref(module, ref)
    # 只接受能对应到真实场景的 id/名字；解析不到就不动，
    # 避免写入脏值后地图回退到「第一个场景」造成「玩家换图了地图却没切」。
    old = session_service.get_char_location(game_session, player_char.id)
    if sid and sid != old:
        if session_service.find_scene_path(module, old, sid) is None:
            reachable = "、".join(
                _scene_name(module, n) for n in session_service.scene_neighbors(module, old)
            )
            note = (
                f"{_scene_name(module, sid)} 与当前场景不连通，无法直接前往"
                + (f"（由此可直达：{reachable}）" if reachable else "")
                + "。请让玩家分步移动或说明为何到不了，不要叙述其已抵达。"
            )
            logger.warning("SCENE_CHANGE 目标不连通，拒绝切换：%s -> %s", old, sid)
            return [], None, note
        # 主角明确移动到新场景：更新其位置（→ current_scene_id、已访问、地图跟随）；
        # 同处一地的队友一同前往，分头在别处的队友留在原地。
        session_service.set_char_location(db, session_id, player_char.id, sid)
        for t in (teammates or []):
            if session_service.get_char_location(game_session, t.id) == old:
                session_service.set_char_location(db, session_id, t.id, sid)
        db.refresh(game_session)
        # 首次抵达新场景 → 追加一张场景配图卡（chunk 随本次切换一并广播/重排）
        chunks = [_make_chunk("system", f"场景切换至：{_scene_name(module, sid)}")]
        chunks += _maybe_scene_illustration(db, session_id, module, sid)
        return chunks, sid, ""
    if not sid:
        logger.warning("SCENE_CHANGE 无法解析场景引用：%r（保持当前场景）", ref)
        return [], None, "场景引用无法解析（保持当前场景）。"
    return [], None, "已身处该场景，未发生切换。"


def _exec_flag(
    db: Session, session_id: str, game_session: GameSession, flag: str, value: bool,
) -> list[str]:
    """置/清剧情标志，并刷新内存里的 world_state 使后续处理立即可见。

    如果当前场景的视觉状态因此发生变化，立即落一张状态配图卡；没有视觉影响的
    flag 不会产生额外图片。
    """
    session_service.set_flag(db, session_id, flag, value)
    db.refresh(game_session)
    label = "剧情推进" if value else "剧情状态解除"
    chunks = [_make_chunk("system", f"{label}：{flag}")]
    module = db.get(Module, game_session.module_id)
    if module is not None:
        chunks.extend(_maybe_scene_illustration(
            db, session_id, module, game_session.current_scene_id,
        ))
    return chunks


async def _exec_handout(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    hid: str, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], str]:
    """发放一份手书（幂等：同 id 只发一次；未知 id 静默跳过）。返回 (chunks, 结果说明)。"""
    handout = next(
        (
            h for h in (getattr(module, "handouts", None) or [])
            if isinstance(h, dict) and str(h.get("id") or "").strip() == hid
        ),
        None,
    )
    if handout is None:
        logger.warning("HANDOUT 指令引用了未知手书 id（跳过）：%r", hid)
        return [], f"未知手书 id：{hid}（只发可发放清单里列出的 id）。"
    db.refresh(game_session)
    if world_memory.handout_issued(game_session.world_state or {}, hid):
        return [], f"手书 {hid} 已发放过（每份只发一次），不再重复。"
    title = str(handout.get("title") or "").strip()
    meta = {
        "kind": "handout",
        "handout_id": hid,
        "title": title,
        "handout_kind": str(handout.get("kind") or "").strip(),
    }
    ev = session_service.add_event(
        db, session_id, "system", str(handout.get("content") or ""),
        actor_name="系统", metadata=meta,
    )
    chunks = [_make_chunk("system", ev.content, metadata=meta, event_id=ev.id)]
    # 世界记忆：记入 handouts_issued（幂等真源）+ 线索台账（status=known，kind=handout），
    # 已发放的手书经台账自然进入后续 KP 上下文、并从「可发放清单」里消失。
    present = [player_char.id] + [t.id for t in (teammates or [])]
    _apply_world_memory(
        db, game_session,
        lambda ws, _hid=hid, _title=title, _seq=ev.sequence_num: (
            world_memory.record_handout_issue(ws, _hid, _title, present, _seq)
        ),
    )
    # 手书配图（可选增强）：激活配置具备生图能力（OpenAI Images 或 ComfyUI）才起后台任务；
    # 卡片即时发出（文字先读），图片生成完经 event_patch 增量补挂，失败静默保持纯文字。
    try:
        if game_session.kp_mode == "human":
            human_kp_service.queue_image_suggestion(
                db, game_session,
                key=f"handout:{module.id}:{hid}",
                title=title or "手书配图",
                prompt=(
                    f"标题：{title}\n类型：{str(handout.get('kind') or '')}\n"
                    f"正文：\n{str(handout.get('content') or '')[:600]}"
                ),
                image_kind="handout",
                image_item_id=hid,
                image_field="image",
                source_event_id=str(ev.id),
            )
            return chunks, f"手书 {hid} 已发放（配图已进入真人 KP 审核队列）。"
        if get_llm().supports_image_gen():
            asyncio.create_task(_illustrate_handout(
                session_id, ev.id, title,
                str(handout.get("kind") or ""), ev.content or "",
            ))
    except Exception:  # noqa: BLE001 — 配图判定失败不影响发放
        logger.exception("手书配图任务启动失败（忽略）")
    return chunks, f"手书 {hid} 已发放（正文已由系统以卡片呈现给玩家，续写时不要复述正文）。"
