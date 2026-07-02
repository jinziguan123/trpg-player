from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.models.module import Module
from app.models.session import GameSession
from app.schemas.event import AdvanceRequest, ChatRequest, CheckRequest, RollRequest, TravelRequest
from app.services import map_service, session_service
from app.services.chat_service import (
    _make_chunk,
    event_to_chunk,
    run_chat_generation,
    run_check_request_generation,
    run_regenerate_generation,
    run_roll_generation,
    run_travel_generation,
    split_ooc,
    split_speech_action,
)
from app.services.generation_manager import generation_manager
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/ooc")
def post_ooc(
    session_id: str,
    data: ChatRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """纯 OOC（场外）消息：入库并向全房间广播，不进入 KP 上下文、不触发任何生成。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    try:
        actor = session_service.resolve_actor(
            db, session_id, token, data.acting_character_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))

    _, ooc = split_ooc(data.content)
    text = ooc or data.content.strip()
    ev = session_service.add_event(
        db, session_id, "ooc", text,
        actor_id=actor.id, actor_name=actor.name,
    )
    room_hub.broadcast(session_id, event_to_chunk(ev))
    return {"ok": True, "id": ev.id}


@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    data: ChatRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """fire-and-forget：校验 + 落库玩家行动并广播 + 触发生成；输出统一经 /live 下发。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")

    try:
        player_char = session_service.resolve_actor(
            db, session_id, token, data.acting_character_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))

    in_character, ooc = split_ooc(data.content)
    if not in_character:
        ev = session_service.add_event(
            db, session_id, "ooc", ooc or data.content.strip(),
            actor_id=player_char.id, actor_name=player_char.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev))
        raise HTTPException(400, "该消息为纯场外发言，请使用 OOC 通道")

    # 正式行动：按引号约定把言（dialogue）与行（action）分流，按原文顺序逐条落库。
    # 引号内=说出口的台词，引号外=行动；不含引号则整条按行动。
    # 玩家事件的广播随生成一起进 in-flight buffer（见 generation_manager.start 的 prelude），
    # 这样断线重连能重放，避免「点了发送但自己的消息没显示、只剩思考中」的吞消息问题。
    # 回合确认制：玩家发言只进入「本回合暂存」（打 pending_turn 标记、实时广播给同桌），
    # 不立即触发 KP。要等所有真人各自点「推进」确认后（见 /advance），才整批交 KP。
    segments = split_speech_action(in_character) or [("action", in_character)]
    for kind, seg_text in segments:
        ev = session_service.add_event(
            db, session_id, kind, seg_text,
            actor_id=player_char.id, actor_name=player_char.name,
            metadata={"pending_turn": True},
        )
        room_hub.broadcast(session_id, event_to_chunk(ev))
    if ooc:
        ev_ooc = session_service.add_event(
            db, session_id, "ooc", ooc,
            actor_id=player_char.id, actor_name=player_char.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev_ooc))

    # 有新发言即撤销本人已有的「确认」（改动后需重新确认），并把最新确认进度广播给同桌。
    session_service.set_turn_confirm(db, session_id, player_char.id, False)
    room_hub.broadcast(
        session_id,
        _make_chunk("turn_state", metadata=session_service.turn_confirm_state(db, session_id)),
    )
    return {"ok": True}


@router.post("/{session_id}/check")
async def check(
    session_id: str,
    data: CheckRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """玩家『申请』技能检定（不指定难度）：交 KP 裁定是否需要、用什么难度。

    KP 判定需要时会挂出「待玩家投骰」的提示，玩家再调 /roll 投骰。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")
    if not data.skill.strip():
        raise HTTPException(400, "未指定检定技能")

    try:
        actor = session_service.resolve_actor(
            db, session_id, token, data.acting_character_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))

    skill = data.skill.strip()
    intent = data.intent.strip()
    # 落一条可见行动记录：申请检定这件事本身要留痕（供其他玩家看到、KP 后续上下文也能看到），
    # 带上 intent 是因为光报技能名时，现场若同时有多条线索/多个可疑点，KP 猜不出具体目标。
    content = f"（申请「{skill}」检定：{intent}）" if intent else f"（申请「{skill}」检定）"
    ev = session_service.add_event(
        db, session_id, "action", content,
        actor_id=actor.id, actor_name=actor.name,
    )
    room_hub.broadcast(session_id, event_to_chunk(ev))
    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(
        session_id,
        run_check_request_generation(session_id, actor.id, skill, intent),
    )
    return {"ok": True}


@router.post("/{session_id}/roll")
async def roll(
    session_id: str,
    data: RollRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """玩家点『投骰』：对一个待定检定掷骰，结果交 KP 据达成等级续写（fire-and-forget）。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")
    if not data.check_id.strip():
        raise HTTPException(400, "未指定检定")

    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(
        session_id,
        run_roll_generation(session_id, data.check_id.strip()),
    )
    return {"ok": True}


@router.post("/{session_id}/regenerate")
async def regenerate(
    session_id: str,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """重新生成最新一轮 KP 叙事：打断卡住的生成 → 回滚上一轮 KP 叙事产物 → 用玩家与队友的既有
    输入（保留已定骰子，不重掷）重跑 KP。

    高风险操作（可能明显改变剧情走向），前端须二次确认后才调用；只作用于「最新一轮」——回滚逻辑
    天然只清理事件流尾部的 KP 产物。
    """
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    # 鉴权：本桌任一真人席位均可触发（无归属的本机会话放行；有归属则须 token 匹配某真人席位）
    human_seats = [p for p in session_service.get_participants(db, session_id) if p.role == "human"]
    if human_seats and not any(
        (not p.owner_token) or (token and p.owner_token == token) for p in human_seats
    ):
        raise HTTPException(403, "无权操作该会话")

    # ①打断卡住/进行中的旧生成（其半截叙事会先落库，②随后被回滚清掉）
    await generation_manager.cancel(session_id)
    removed = session_service.rollback_last_kp_output(db, session_id)

    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(session_id, run_regenerate_generation(session_id))
    return {"ok": True, "removed": removed}


@router.post("/{session_id}/advance")
async def advance(
    session_id: str,
    data: AdvanceRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """玩家点『推进本回合』：记录该真人的确认；所有真人都确认后，把本回合暂存发言整批交 KP
    （先跑 AI 队友回合，再 KP 叙事）。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")
    try:
        actor = session_service.resolve_actor(db, session_id, token, data.acting_character_id)
    except ValueError as e:
        raise HTTPException(403, str(e))

    session_service.set_turn_confirm(db, session_id, actor.id, True)
    state = session_service.turn_confirm_state(db, session_id)
    room_hub.broadcast(session_id, _make_chunk("turn_state", metadata=state))

    if state["ready"]:
        # 所有真人已确认：暂存发言转正 + 清确认，然后触发一轮（队友回合 + KP）。
        session_service.commit_turn(db, session_id)
        room_hub.broadcast(session_id, _make_chunk("generating"))
        generation_manager.start(session_id, run_chat_generation(session_id))
    return {"ok": True, "ready": state["ready"]}


@router.get("/{session_id}/search")
def search_history(session_id: str, q: str = "", db: Session = Depends(get_db)):
    """在本局历史里模糊检索，返回匹配事件（含 sequence_num 供前端定位/跳转）。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    rows = session_service.search_events(db, session_id, q)
    return {
        "results": [
            {
                "id": e.id,
                "sequence_num": e.sequence_num,
                "event_type": e.event_type,
                "actor_name": e.actor_name or "",
                "content": (e.content or "")[:140],
            }
            for e in rows
        ]
    }


@router.get("/{session_id}/scene-map")
def scene_map(session_id: str, char_id: str | None = None, db: Session = Depends(get_db)):
    """某角色所在场景的（按剧情 flags 解析后的）像素地图 + 实体位置，供游戏内地图面板渲染。

    char_id 给定时（前端传当前用户角色）地图跟随该角色所在场景——分头行动时各看各的。
    """
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    return map_service.current_scene_map(db, game_session, char_id=char_id)


@router.get("/{session_id}/locations")
def locations(session_id: str, char_id: str | None = None, db: Session = Depends(get_db)):
    """大地图：已知地点列表（已访问 ∪ 与之相连的场景；未探索的不显示），含当前所在标记。"""
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    module = db.get(Module, game_session.module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    events = session_service.get_session_events(db, session_id)
    return {"locations": session_service.list_known_locations(module, game_session, char_id=char_id, events=events)}


@router.post("/{session_id}/travel")
async def travel(
    session_id: str,
    data: TravelRequest,
    db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """玩家经大地图『前往』某已知地点：确定性切换该玩家所在场景，再由 KP 叙述抵达见闻。

    场景切换由玩家显式发起（而非 KP 据只言片语臆测），杜绝「说句话就被自动搬走」。
    """
    game_session = db.get(GameSession, session_id)
    if not game_session:
        raise HTTPException(404, "会话不存在")
    if game_session.status != "active":
        raise HTTPException(400, "会话未处于活跃状态")
    if generation_manager.is_generating(session_id):
        raise HTTPException(409, "KP 正在叙事，请稍候")
    try:
        actor = session_service.resolve_actor(db, session_id, token, data.acting_character_id)
    except ValueError as e:
        raise HTTPException(403, str(e))

    module = db.get(Module, game_session.module_id)
    scene_id = (data.scene_id or "").strip()
    events = session_service.get_session_events(db, session_id)
    known = session_service.known_scene_ids(module, game_session, events) if module else set()
    if scene_id not in known:
        raise HTTPException(400, "该地点尚未知晓或不可前往")
    if session_service.get_char_location(game_session, actor.id) == scene_id:
        raise HTTPException(400, "你已身处该地点")

    generation_manager.start(
        session_id, run_travel_generation(session_id, actor.id, scene_id),
    )
    return {"ok": True}
