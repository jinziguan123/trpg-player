"""把回合规划器的结构化裁定确定性落实为领域副作用。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai import turn_planner
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import (
    inventory_service,
    kp_actions,
    session_service,
    turn_context,
    turn_effects,
)
from app.services.event_protocol import make_chunk as _make_chunk

logger = logging.getLogger(__name__)
_current_turn_events = turn_context._current_turn_events
_exec_start_combat = kp_actions._exec_start_combat
_exec_san_check = turn_effects._exec_san_check
_exec_hp_change = turn_effects._exec_hp_change
_resolve_hp_target = turn_effects._resolve_hp_target
_exec_scene_change = turn_effects._exec_scene_change


async def _ensure_planned_combat(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    teammates: list[Character] | None,
    llm,
    plan: turn_planner.TurnPlan | None,
) -> AsyncIterator[str]:
    """确保规划器裁定的开战一定落成战斗态，补偿 KP 漏调工具或旧指令。

    模型仍负责识别「是否开战、敌方是谁」；一旦结构化计划给出肯定裁定，状态切换就由
    后端确定性保证。若 KP 已经通过工具或文本指令创建战斗，本守卫幂等返回。
    """
    if plan is None or not plan.combat.should_start:
        return

    from app.services import combat_service

    current = combat_service.get_combat(db.get(GameSession, session_id))
    if current and current.get("active"):
        return

    enemies = "，".join(plan.combat.enemies)
    trigger = plan.combat.trigger.strip() or plan.player_intent.strip() or "冲突升级为正面交战"
    if not enemies:
        logger.warning("规划器裁定开战但未给敌方名字，使用临场敌人兜底: session=%s", session_id)
    chunks = await _exec_start_combat(
        db, session_id, game_session, module, player_char, teammates, llm, enemies, trigger,
    )
    for chunk in chunks:
        yield chunk

def _san_rolled_this_turn(db: Session, session_id: str, pre_gen_seq: int) -> bool:
    """本轮生成里是否已产生过 SAN 骰点事件（seq > 生成前基线且 metadata.skill=='SAN'）——
    用于让确定性 SAN 守卫在 KP 已自行掷过 SAN 时幂等跳过，不重复扣。"""
    for ev in session_service.get_session_events(db, session_id):
        if (
            (ev.sequence_num or 0) > pre_gen_seq
            and ev.event_type == "dice"
            and (ev.metadata_ or {}).get("skill") == "SAN"
        ):
            return True
    return False


async def _ensure_planned_sanity(
    db: Session,
    session_id: str,
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
    plan: turn_planner.TurnPlan | None,
    pre_gen_seq: int,
) -> AsyncIterator[str]:
    """确保规划器裁定的『目睹恐怖』一定落成理智检定，补偿 KP 漏发 SAN_CHECK。

    模型仍负责识别本轮是否目睹恐怖及其强度；一旦结构化计划肯定裁定，SAN 检定就由后端确定性
    发出（系统自动掷、结算损失与疯狂）。若 KP 本轮已自行掷过 SAN（任意恐怖源），本守卫幂等跳过；
    同一角色对同一恐怖源的去重仍由 _exec_san_check（world_state.san_checked）保证。
    """
    if plan is None or not plan.sanity.trigger:
        return
    if _san_rolled_this_turn(db, session_id, pre_gen_seq):
        return
    kv = {
        "success_loss": plan.sanity.success_loss or "0",
        "failure_loss": plan.sanity.failure_loss or "1d6",
        "source": (plan.sanity.source or "本轮目睹的恐怖").strip(),
        "chars": "/".join(plan.sanity.witnesses) if plan.sanity.witnesses else "",
    }
    chunks, _descs = await _exec_san_check(
        db, session_id, game_session, kv, player_char, teammates,
    )
    for chunk in chunks:
        yield chunk


def _hp_changed_this_turn(db: Session, session_id: str, pre_gen_seq: int) -> bool:
    """本轮生成里是否已发生过**扣血**事件（KP 自发 HP_CHANGE 或先前守卫）——用于让大失败反噬守卫
    在 KP 已自行扣血时幂等跳过，不重复伤害。只认伤害（hp_change<0），治疗不算。"""
    for ev in session_service.get_session_events(db, session_id):
        if (ev.sequence_num or 0) > pre_gen_seq and ((ev.metadata_ or {}).get("hp_change") or 0) < 0:
            return True
    return False


async def _ensure_planned_mishap(
    db: Session,
    session_id: str,
    player_char: Character,
    teammates: list[Character] | None,
    plan: turn_planner.TurnPlan | None,
    pre_gen_seq: int,
) -> AsyncIterator[str]:
    """确保规划器裁定的『大失败身体反噬』一定落成扣血，补偿 KP 漏发 HP_CHANGE。

    仅大失败且所做动作本身有身体危险时，planner 才置 mishap.trigger（图书馆/话术等无害失败不触发）。
    KP 本轮已自行扣过血则幂等跳过，不重复伤害；受伤者按 plan 指定，缺省本轮掷骰玩家。
    """
    if plan is None or not plan.mishap.trigger:
        return
    delta = int(plan.mishap.hp_delta or 0)
    if delta >= 0:                                          # 恒为伤害；非负=无有效反噬
        return
    if _hp_changed_this_turn(db, session_id, pre_gen_seq):  # KP 已自行扣血 → 不重复
        return
    target = (plan.mishap.target or player_char.name).strip()
    reason = (plan.mishap.reason or "大失败反噬").strip()
    chunks = await _exec_hp_change(
        db, session_id, player_char, target, str(delta), reason, teammates=teammates,
    )
    for chunk in chunks:
        yield chunk


async def _ensure_planned_items(
    db: Session,
    session_id: str,
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
    plan: turn_planner.TurnPlan | None,
) -> AsyncIterator[str]:
    """规划器裁定的物品增减确定性落库（获得入库、失去/消耗移除），补偿 KP 不记账——库存是权威状态。

    幂等：按「本轮玩家行动锚序号 + 获/失 + 名字 + 角色」去重（存 world_state.item_delta_keys），
    重新生成不会重复增减。物品效果仍由 KP 叙述——这里只保证库存数目可靠。
    """
    if plan is None or (not plan.items_gained and not plan.items_lost):
        return
    turn = _current_turn_events(session_service.get_session_events(db, session_id))
    anchor = max(
        (e.sequence_num or 0 for e in turn if e.event_type in ("action", "dialogue")),
        default=0,
    )
    ws = dict(game_session.world_state or {})
    done = set(ws.get("item_delta_keys") or [])
    changed = False

    def _who(name: str) -> Character:
        return _resolve_hp_target((name or "").strip(), player_char, teammates) or player_char

    for ig in plan.items_gained:
        name = (ig.name or "").strip()
        if not name:
            continue
        target = _who(ig.who)
        key = f"g|{anchor}|{name}|{target.id}"
        if key in done:
            continue
        inventory_service.add_item(db, target, name, qty=ig.qty or 1, kind=(ig.kind or None))
        done.add(key); changed = True
        suffix = f"×{ig.qty}" if (ig.qty or 1) > 1 else ""
        ev = session_service.add_event(
            db, session_id, "system", f"{target.name} 获得了 {name}{suffix}",
            actor_name="系统", metadata={"item_gain": True, "char_id": target.id},
        )
        yield _make_chunk("system", ev.content, event_id=ev.id, metadata={"item_gain": True})
        yield _make_chunk("inventory_update", metadata={"char_id": target.id})

    for il in plan.items_lost:
        name = (il.name or "").strip()
        if not name:
            continue
        target = _who(il.who)
        key = f"l|{anchor}|{name}|{target.id}"
        if key in done:
            continue
        done.add(key); changed = True   # 记键即便无匹配，避免重生成反复尝试
        if inventory_service.remove_by_name(db, target, name, qty=il.qty or 1):
            ev = session_service.add_event(
                db, session_id, "system", f"{target.name} 失去了 {name}",
                actor_name="系统", metadata={"item_loss": True, "char_id": target.id},
            )
            yield _make_chunk("system", ev.content, event_id=ev.id, metadata={"item_loss": True})
            yield _make_chunk("inventory_update", metadata={"char_id": target.id})

    if changed:
        ws["item_delta_keys"] = list(done)
        game_session.world_state = ws
        db.commit()


async def _ensure_planned_combat_damage(
    db: Session,
    session_id: str,
    player_char: Character,
    plan: turn_planner.TurnPlan | None,
) -> AsyncIterator[str]:
    """战斗中非常规/范围攻击（燃烧弹/群体/环境）→ 引擎把伤害挂成玩家 pending_roll（亲手掷、
    应用到所有波及敌人）。仅战斗中生效；幂等（stage_aoe_damage 按行动锚 dedup_key 去重）。"""
    if plan is None or not plan.combat_damage.trigger or not plan.combat_damage.targets:
        return
    from app.services import combat_service
    if not combat_service.get_combat(db.get(GameSession, session_id)):
        return
    turn = _current_turn_events(session_service.get_session_events(db, session_id))
    anchor = max(
        (e.sequence_num or 0 for e in turn if e.event_type in ("action", "dialogue")), default=0)
    cd = plan.combat_damage
    chunk, staged = combat_service.stage_aoe_damage(
        db, session_id, player_char.id, list(cd.targets), cd.weapon, cd.formula, cd.burning,
        cd.reason, dedup_key=f"{anchor}|{'/'.join(cd.targets)}",
    )
    if staged and chunk:
        yield chunk


async def _ensure_planned_scene(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    teammates: list[Character] | None,
    plan: turn_planner.TurnPlan | None,
) -> AsyncIterator[str]:
    """确保规划器裁定的『玩家本轮真实移动到某场景』一定落成位置/地图切换，补偿 KP 漏调 scene_change。

    这修复的是「KP 叙述了到达新场景，但大地图仍停在旧场景」——过去场景切换**只**靠 KP 记得发
    `[SCENE_CHANGE]`/`scene_change` 工具，漏发就地图与叙事脱节。现在与 SAN/战斗/库存一致：规划器
    给出明确目标场景，后端确定性把角色搬过去。

    幂等且保守：
    - KP 已自行切到目标场景 → `_exec_scene_change` 见位置已到位、原地返回，不重复切；
    - 目标解析不到真实场景 id/名 → 安全跳过（不写脏值、不回退到首个场景）；
    - 规划器仅在玩家**确实前往并到达**别处时才置此字段（『讨论/打算去』不置），语义与 KP 工具一致。
    """
    if plan is None:
        return
    ref = (plan.scene_policy.scene_change or "").strip()
    if not ref:
        return
    db.refresh(game_session)
    chunks, _sid, _note = await _exec_scene_change(
        db, session_id, game_session, module, ref, player_char, teammates,
    )
    for chunk in chunks:
        yield chunk
