"""战斗态端点（P2）：玩家提交战斗行动 + 读取当前战斗态。

行动经先攻队列校验（只有轮到的真人可提交），结算后广播 combat/dice/system chunks，
并把随后的 NPC 回合自动推进到下一个真人回合或战斗结束。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.schemas.combat import ChaseActionRequest, CombatActionRequest, ReactionRequest
from app.services import chase_service, combat_service, session_service
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["combat"])


def _actor_char_id(db: Session, session_id: str, token: str | None) -> str | None:
    """token 对应的席位角色 id（即战斗参战方 id）。"""
    for p in session_service.get_participants(db, session_id):
        if p.owner_token and token and p.owner_token == token:
            return p.character_id
    return None


@router.get("/{session_id}/combat")
def get_combat(session_id: str, db: Session = Depends(get_db)):
    """当前战斗态（无则 {active:false}），供前端渲染战斗视图与断线重连对齐。"""
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
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
    """提交一个战斗行动（attack/dodge/fight_back/flee/other）。仅当轮到本玩家时有效。"""
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    char_id = _actor_char_id(db, session_id, token)
    # 兼容纯本机会话（无 token 归属）：回落到会话主角
    actor_id = char_id or session.player_character_id
    if not actor_id:
        raise HTTPException(403, "无法确定行动角色")
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
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    # 兼容纯本机会话（无 token 归属）：回落到会话主角
    actor_id = _actor_char_id(db, session_id, token) or session.player_character_id
    if not actor_id:
        raise HTTPException(403, "无法确定行动角色")
    try:
        chunks = await combat_service.resolve_reaction(
            db, session_id, actor_id, data.choice, agent=_combat_agent(db, session))
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    return {"ok": True}


def _combat_agent(db: Session, session):
    """构建战斗子代理（有可用 AI 配置时）；无则 None → 纯机械结算。"""
    try:
        from app.ai.agents.combat_agent import CombatAgent
        from app.ai.llm_factory import get_llm
        return CombatAgent(get_llm())
    except Exception:
        return None


@router.get("/{session_id}/chase")
def get_chase(session_id: str, db: Session = Depends(get_db)):
    """当前追逐态（无则 {active:false}），供前端渲染距离轨与重连对齐。"""
    session = session_service.get_session(db, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
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
    if not session_service.get_session(db, session_id):
        raise HTTPException(404, "会话不存在")
    try:
        chunks = await chase_service.resolve_chase_round(
            db, session_id, data.model_dump(exclude_none=True),
            agent=_combat_agent(db, None), scene_hint="",
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    for chunk in chunks:
        room_hub.broadcast(session_id, chunk)
    return {"ok": True}
