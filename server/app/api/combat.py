"""战斗态端点（P2）：玩家提交战斗行动 + 读取当前战斗态。

行动经先攻队列校验（只有轮到的真人可提交），结算后广播 combat/dice/system chunks，
并把随后的 NPC 回合自动推进到下一个真人回合或战斗结束。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import (
    player_token,
    require_session_token_actor,
    require_session_viewer,
)
from app.database import get_db
from app.schemas.combat import ChaseActionRequest, CombatActionRequest, ReactionRequest
from app.services import chase_service, combat_service, session_service
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["combat"])


@router.get("/{session_id}/combat")
def get_combat(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """当前战斗态（无则 {active:false}），供前端渲染战斗视图与断线重连对齐。"""
    session = require_session_viewer(db, session_id, token)
    state = combat_service.get_combat(session)
    if not state:
        return {"active": False}
    return combat_service._combat_meta(state) | {"active": True}


@router.post("/{session_id}/combat/action")
async def combat_action(
    session_id: str,
    data: CombatActionRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """提交一个战斗行动（attack/dodge/fight_back/flee/first_aid/observe/maneuver/reload/aim/other）。
    仅当轮到本玩家时有效。"""
    actor = require_session_token_actor(db, session_id, token)
    session = session_service.get_session(db, session_id)
    actor_id = actor.id
    # 方格移动：常规移动(move)不推进先攻、同回合仍可攻击；冲刺(dash)独占本回合 → 推进先攻并续跑 NPC 驱动。
    if data.type in ("move", "dash"):
        dash = data.type == "dash"
        try:
            chunks = combat_service.resolve_move(db, session_id, actor_id, data.dest or {}, dash=dash)
        except ValueError as e:
            raise HTTPException(409, str(e))
        if dash:   # 冲刺用掉本回合 → 驱动 NPC 到下一个真人回合/战斗结束
            state = combat_service.get_combat(session_service.get_session(db, session_id))
            if state:
                drive_chunks, _ = await combat_service.drive_npcs(
                    db, session_id, state, agent=_combat_agent(db, session))
                chunks += drive_chunks
        for chunk in chunks:
            room_hub.broadcast(session_id, chunk)
        _schedule_aftermath_if_ended(session_id, chunks, session)
        return {"ok": True}
    try:
        agent = _combat_agent(db, session)
        chunks = await combat_service.resolve_player_action(
            db, session_id, actor_id, data.model_dump(exclude_none=True),
            agent=agent, scene_hint="",
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    _schedule_aftermath_if_ended(session_id, chunks, session)
    return {"ok": True}


@router.post("/{session_id}/combat/roll")
async def combat_roll(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """两段式攻击第二段：玩家亲自掷伤害。仅当有属于本玩家的 pending_roll 时有效。"""
    actor = require_session_token_actor(db, session_id, token)
    session = session_service.get_session(db, session_id)
    actor_id = actor.id
    try:
        chunks = await combat_service.resolve_combat_roll(
            db, session_id, actor_id, agent=_combat_agent(db, session), scene_hint="")
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    _schedule_aftermath_if_ended(session_id, chunks, session)
    return {"ok": True}


@router.post("/{session_id}/combat/reaction")
async def combat_reaction(
    session_id: str,
    data: ReactionRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """被攻击的真人提交反应（fight_back/dodge/cover）。token 对应的角色即防御者。

    结算这一击并续跑驱动，广播新的 combat_state/dice/narration。
    """
    actor = require_session_token_actor(db, session_id, token)
    session = session_service.get_session(db, session_id)
    actor_id = actor.id
    try:
        chunks = await combat_service.resolve_reaction(
            db, session_id, actor_id, data.choice, agent=_combat_agent(db, session))
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    _schedule_aftermath_if_ended(session_id, chunks, session)
    return {"ok": True}


def _combat_agent(db: Session, session):
    """构建战斗子代理（有可用 AI 配置时）；无则 None → 纯机械结算。"""
    if session is not None and getattr(session, "kp_mode", "ai") == "human":
        return None
    try:
        from app.ai.agents.combat_agent import CombatAgent
        from app.ai.llm_factory import get_llm
        return CombatAgent(get_llm())
    except Exception:
        return None


def _schedule_aftermath_if_ended(session_id: str, chunks: list[str], session=None) -> None:
    """本次行动使战斗/追逐结束（chunks 含 combat_end 或 chase_end）→ 主动调度一次主 KP 余波生成。

    不必等玩家先开口：KP 读 combat_result（战斗与追逐共用此折回通道）承接后果、把场面交还调查员。
    已有生成在跑则跳过（下一玩家回合仍会折回，不丢）。
    """
    if getattr(session, "kp_mode", "ai") == "human":
        return
    if not any(('"combat_end"' in c or '"chase_end"' in c) for c in chunks):
        return
    from app.services.event_protocol import make_chunk as _make_chunk
    from app.services.generation_manager import generation_manager
    from app.services.turn_orchestrator import run_combat_aftermath_generation
    if generation_manager.is_generating(session_id):
        return
    coro = run_combat_aftermath_generation(session_id)
    try:
        # prelude 带 generating：与其它生成入口对齐，让前端立刻亮起「KP 叙事中」指示器
        generation_manager.start(session_id, coro, prelude=[_make_chunk("generating")])
    except ValueError:
        coro.close()   # 竞态：刚好有生成在跑，放弃本次余波（下一玩家回合仍会折回）


@router.get("/{session_id}/chase")
def get_chase(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """当前追逐态（无则 {active:false}），供前端渲染距离轨与重连对齐。"""
    session = require_session_viewer(db, session_id, token)
    state = chase_service.get_chase(session)
    if not state:
        return {"active": False}
    return chase_service._meta(state) | {"active": True}


@router.post("/{session_id}/chase/action")
async def chase_action(
    session_id: str,
    data: ChaseActionRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """玩家推进一轮追逐（奔逃/闯障）。结算后广播 dice/chase/system chunks。"""
    require_session_token_actor(db, session_id, token)
    session = session_service.get_session(db, session_id)
    try:
        chunks = await chase_service.resolve_chase_round(
            db, session_id, data.model_dump(exclude_none=True),
            agent=_combat_agent(db, session), scene_hint="",
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    _schedule_aftermath_if_ended(session_id, chunks, session)
    return {"ok": True}
