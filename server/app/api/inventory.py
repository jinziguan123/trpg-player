"""玩家侧库存操作：查看 / 使用 / 丢弃 / 转让。确定性执行——库存是权威状态。

- 使用：消耗品自动 -1，并把「（使用道具：X）」作为本回合暂存动作加入，效果交 KP 叙述；
- 丢弃/转让：纯确定性移动，广播 inventory_update 让各端刷新（不触发生成）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import (
    player_token,
    require_session_actor,
    require_session_viewer,
)
from app.database import get_db
from app.models.character import Character
from app.schemas.event import (
    InventoryDropRequest,
    InventoryGiveRequest,
    InventoryUseRequest,
)
from app.services import inventory_service, session_service
from app.services.event_protocol import event_to_chunk, make_chunk as _make_chunk
from app.services.room_hub import room_hub

router = APIRouter(prefix="/api/sessions", tags=["inventory"])


def _inv_update_chunk(char_id: str) -> str:
    return _make_chunk("inventory_update", metadata={"char_id": char_id})


@router.get("/{session_id}/inventory")
def get_inventory(
    session_id: str, char_id: str, db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    """读取某角色的活库存（供前端「道具」页签渲染）。

    惰性播种：旧存档/未开场角色的活库存为空时，从角色卡静态 equipment 播种一次——
    这样开场前建的会话也能立刻有带 id 的活库存（可用/给/丢），无需先走一回合。
    """
    session = require_session_viewer(db, session_id, token)
    party_ids = {
        p.character_id
        for p in session_service.get_participants(db, session_id)
        if p.character_id
    }
    if session.player_character_id:
        party_ids.add(session.player_character_id)
    if char_id not in party_ids:
        raise HTTPException(403, "角色不属于该会话")
    char = db.get(Character, char_id)
    if char is None:
        raise HTTPException(404, "角色不存在")
    inventory_service.seed_from_equipment(db, char)   # 幂等：非空则跳过
    return {"items": inventory_service.get_inventory(char)}


@router.post("/{session_id}/inventory/use")
def use_item(
    session_id: str, data: InventoryUseRequest, db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    actor = require_session_actor(
        db, session_id, token, data.acting_character_id,
    )
    used = inventory_service.use_item(db, actor, data.item_id)
    if used is None:
        raise HTTPException(400, "该物品不在你的库存中")
    # 把「使用」作为本回合暂存动作加入 → 推进时 KP 据此叙述效果（消耗已由引擎确定性结算）。
    ev = session_service.add_event(
        db, session_id, "action", f"（使用道具：{used['name']}）",
        actor_id=actor.id, actor_name=actor.name,
        metadata={"pending_turn": True, "item_use": True, "item_name": used["name"]},
    )
    room_hub.broadcast(session_id, event_to_chunk(ev))
    session_service.set_turn_confirm(db, session_id, actor.id, False)
    room_hub.broadcast(session_id, _make_chunk(
        "turn_state", metadata=session_service.turn_confirm_state(db, session_id)))
    room_hub.broadcast(session_id, _inv_update_chunk(actor.id))
    return {"ok": True, "used": used}


@router.post("/{session_id}/inventory/drop")
def drop_item(
    session_id: str, data: InventoryDropRequest, db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    actor = require_session_actor(
        db, session_id, token, data.acting_character_id,
    )
    removed = inventory_service.remove_item(db, actor, data.item_id, qty=data.qty)
    if removed is None:
        raise HTTPException(400, "该物品不在你的库存中")
    room_hub.broadcast(session_id, _inv_update_chunk(actor.id))
    return {"ok": True, "dropped": removed}


@router.post("/{session_id}/inventory/give")
def give_item(
    session_id: str, data: InventoryGiveRequest, db: Session = Depends(get_db),
    token: str | None = Depends(player_token),
):
    actor = require_session_actor(
        db, session_id, token, data.acting_character_id,
    )
    # 收礼方必须是本会话同队角色
    party_ids = {c.id for c in session_service.get_party_members(db, session_id)}
    if data.to_character_id not in party_ids:
        raise HTTPException(400, "对方不在本队")
    to_char = db.get(Character, data.to_character_id)
    if to_char is None:
        raise HTTPException(404, "对方角色不存在")
    moved = inventory_service.give_item(db, actor, to_char, data.item_id, qty=data.qty)
    if moved is None:
        raise HTTPException(400, "该物品不在你的库存中")
    room_hub.broadcast(session_id, _inv_update_chunk(actor.id))
    room_hub.broadcast(session_id, _inv_update_chunk(to_char.id))
    return {"ok": True, "given": moved, "to": to_char.name}
