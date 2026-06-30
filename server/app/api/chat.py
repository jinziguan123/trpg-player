from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import player_token
from app.database import get_db
from app.models.module import Module
from app.models.session import GameSession
from app.schemas.event import ChatRequest, CheckRequest, RollRequest, TravelRequest
from app.services import map_service, session_service
from app.services.chat_service import (
    _make_chunk,
    event_to_chunk,
    run_chat_generation,
    run_check_request_generation,
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
    segments = split_speech_action(in_character) or [("action", in_character)]
    prelude: list[str] = []
    for kind, seg_text in segments:
        ev = session_service.add_event(
            db, session_id, kind, seg_text,
            actor_id=player_char.id, actor_name=player_char.name,
        )
        prelude.append(event_to_chunk(ev))
    if ooc:
        ev_ooc = session_service.add_event(
            db, session_id, "ooc", ooc,
            actor_id=player_char.id, actor_name=player_char.name,
        )
        prelude.append(event_to_chunk(ev_ooc))

    prelude.append(_make_chunk("generating"))
    generation_manager.start(session_id, run_chat_generation(session_id), prelude=prelude)
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

    room_hub.broadcast(session_id, _make_chunk("generating"))
    generation_manager.start(
        session_id,
        run_check_request_generation(session_id, actor.id, data.skill.strip()),
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
