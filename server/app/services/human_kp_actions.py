"""真人 KP 工具桌到确定性领域动作的适配。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import (
    dice_runtime,
    human_kp_service,
    illustration_service,
    kp_actions,
    session_service,
    turn_context,
    turn_effects,
)
from app.services.event_protocol import event_to_chunk, make_chunk as _make_chunk

_resolve_opposed = dice_runtime._resolve_opposed
_exec_generic_roll = dice_runtime._exec_generic_roll
_attach_npc_portrait = illustration_service._attach_npc_portrait
_scene_name = turn_context._scene_name
_exec_dice_check = turn_effects._exec_dice_check
_exec_scene_change = turn_effects._exec_scene_change
_exec_flag = turn_effects._exec_flag
_exec_handout = turn_effects._exec_handout
_exec_hp_change = turn_effects._exec_hp_change
_exec_san_check = turn_effects._exec_san_check
_exec_start_combat = kp_actions._exec_start_combat


async def execute_human_kp_action(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    action: str,
    payload: dict,
) -> tuple[list[str], str]:
    """真人 KP M1 工具桌：把表单动作路由到既有确定性执行器。

    这里不生成 AI 叙事，也不复制骰子、场景、战斗和库存规则；只负责把真人 KP 的
    触发转换为现有执行器调用，并返回待广播的 chunks 与简短结果。
    """
    player_char = human_kp_service.resolve_player_character(
        db, session_id, game_session,
    )
    if player_char is None:
        raise ValueError("会话缺少主角角色，无法执行 KP 动作")
    teammates = session_service.get_party_members(
        db, session_id, exclude_id=player_char.id,
    )
    action = str(action or "").strip()
    payload = payload if isinstance(payload, dict) else {}

    if action == "narration":
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("叙事内容不能为空")
        ev = session_service.add_event(
            db, session_id, "narration", content, actor_name="KP",
            metadata={"kp_manual": True},
        )
        return [event_to_chunk(ev)], "叙事已发布"

    if action == "dialogue":
        ref = str(payload.get("npc_id") or payload.get("actor_name") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not ref or not content:
            raise ValueError("NPC 与台词内容不能为空")
        npc = next(
            (
                n for n in (module.npcs or [])
                if str(n.get("id") or "") == ref or str(n.get("name") or "") == ref
            ),
            None,
        )
        actor_id = str(npc.get("id")) if npc and npc.get("id") else None
        actor_name = str(npc.get("name") or ref) if npc else ref
        ev = session_service.add_event(
            db, session_id, "dialogue", content,
            actor_id=actor_id, actor_name=actor_name,
            metadata={"kp_manual": True},
        )
        if npc:
            _attach_npc_portrait(db, session_id, module, ev)
        metadata = {"portrait": (ev.metadata_ or {}).get("portrait")} if (ev.metadata_ or {}).get("portrait") else None
        return [
            _make_chunk(
                "dialogue", content, actor_name=actor_name, actor_id=actor_id,
                event_id=ev.id, metadata=metadata,
            )
        ], "NPC 台词已发布"

    if action == "dice_check":
        kv = {str(k): str(v) for k, v in payload.items() if v is not None}
        if not kv.get("skill", "").strip():
            raise ValueError("检定技能不能为空")
        chunks, descs, pending = await _exec_dice_check(
            db, session_id, game_session, module, kv, player_char, teammates,
        )
        return chunks, "已发起待投检定" if pending else ("；".join(descs) or "检定已结算")

    if action == "opposed_check":
        descs: list[str] = []
        chunks = [
            c async for c in _resolve_opposed(
                db, session_id, payload, get_engine(module.rule_system),
                module, player_char, teammates, descs,
            )
        ]
        if not descs:
            raise ValueError("对抗检定至少需要双方角色与技能")
        return chunks, "；".join(descs)

    if action == "generic_roll":
        return _exec_generic_roll(db, session_id, module, payload)

    if action == "scene_change":
        chunks, sid, note = await _exec_scene_change(
            db, session_id, game_session, module,
            str(payload.get("scene_id") or payload.get("scene") or "").strip(),
            player_char, teammates,
        )
        return chunks, note or (f"已切换至 {_scene_name(module, sid)}" if sid else "场景未变化")

    if action in ("set_flag", "clear_flag"):
        flag = str(payload.get("flag") or "").strip()
        if not flag:
            raise ValueError("剧情标志不能为空")
        return _exec_flag(db, session_id, game_session, flag, action == "set_flag"), "剧情标志已更新"

    if action == "handout":
        hid = str(payload.get("id") or payload.get("handout_id") or "").strip()
        if not hid:
            raise ValueError("手书 ID 不能为空")
        return await _exec_handout(
            db, session_id, game_session, module, hid, player_char, teammates,
        )

    if action == "hp_change":
        chunks = await _exec_hp_change(
            db, session_id, player_char,
            str(payload.get("target") or ""), str(payload.get("delta") or ""),
            str(payload.get("reason") or ""), module=module, teammates=teammates,
        )
        if not chunks:
            raise ValueError("HP 目标或变化值无效")
        return chunks, "HP 已结算"

    if action == "san_check":
        chunks, descs = await _exec_san_check(
            db, session_id, game_session, payload, player_char, teammates,
        )
        return chunks, "；".join(descs) if descs else "本次理智检定无需重复结算"

    if action == "start_combat":
        enemies = str(payload.get("enemies") or "").strip()
        if not enemies:
            raise ValueError("至少指定一个敌人")
        chunks = await _exec_start_combat(
            db, session_id, game_session, module, player_char, teammates,
            None, enemies, str(payload.get("trigger") or "真人 KP 发起战斗"),
        )
        return chunks, "已切入结构化战斗"

    raise ValueError(f"不支持的 KP 动作：{action}")
