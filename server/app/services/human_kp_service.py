"""真人 KP 私有工作区与 AI 参谋。

本模块只产生 KP 私有响应；只有显式 publish/team-turn 动作会进入公共事件流。
"""

from __future__ import annotations

from dataclasses import asdict
import logging
import re

from sqlalchemy.orm import Session

from app.ai import director_signals, turn_planner
from app.ai.context import build_kp_context
from app.ai.llm_factory import get_fast_llm, get_llm
from app.models.character import Character
from app.models.module import Module, ModuleChunk
from app.models.session import GameSession
from app.services import (
    image_store,
    module_image_service,
    module_rag_service,
    rulebook_service,
    session_service,
)
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)

_IMAGE_STYLE = (
    "monochrome manga illustration, bold ink lineart, cross-hatching and screentone shading, "
    "mostly black and white with sparse desaturated color accent, gritty dark comic style"
)
_CONTROL_TAG_RE = re.compile(r"\[(?:[A-Z_]{3,})(?::[^\]]*)?\]")


def resolve_player_character(
    db: Session, session_id: str, game_session: GameSession,
) -> Character | None:
    """解析真人 KP 上下文中的玩家角色，兼容 KP 独占 KP 席位的会话。"""
    player = (
        db.get(Character, game_session.player_character_id)
        if game_session.player_character_id
        else None
    )
    if player is None:
        # 真人 KP 新身份模型允许创建者只占 KP 席，主角席暂时为空；
        # 优先用第一名真人角色作锚点；全 AI 玩家席（KP 独走局）则回落到
        # 第一个已入座的 AI 队友——KP 工具（参谋/动作执行）都需要这个上下文锚。
        parts = session_service.get_participants(db, session_id)
        human_ids = [p.character_id for p in parts if p.role == "human" and p.character_id]
        any_ids = [p.character_id for p in parts if p.role != "kp" and p.character_id]
        anchor = human_ids[0] if human_ids else (any_ids[0] if any_ids else None)
        if anchor:
            player = db.get(Character, anchor)
    return player


def _party(
    db: Session, session_id: str, game_session: GameSession,
) -> tuple[Character, list[Character]]:
    player = resolve_player_character(db, session_id, game_session)
    if player is None:
        raise ValueError("会话缺少主角角色")
    others = session_service.get_party_members(
        db, session_id, exclude_id=player.id,
    )
    return player, others


def _private_state(game_session: GameSession) -> dict:
    return dict(game_session.kp_state or {})


def queue_image_suggestion(
    db: Session,
    game_session: GameSession,
    *,
    key: str,
    title: str,
    prompt: str,
    image_kind: str,
    image_item_id: str = "",
    image_field: str = "",
    variant_key: str = "",
    preview_url: str = "",
    source_event_id: str = "",
) -> dict:
    """把自动配图意图放进真人 KP 私有队列；同一稳定键只保留一条。"""
    if game_session.kp_mode != "human":
        return {}
    stable_key = key.strip()
    if not stable_key:
        return {}
    state = _private_state(game_session)
    suggestions = [
        dict(item) for item in (state.get("image_suggestions") or [])
        if isinstance(item, dict) and str(item.get("key") or "") != stable_key
    ]
    suggestion = {
        "key": stable_key,
        "title": title.strip() or "KP 配图",
        "prompt": prompt.strip()[:2000],
        "image_kind": image_kind,
        "image_item_id": image_item_id,
        "image_field": image_field,
    }
    if variant_key:
        suggestion["variant_key"] = variant_key
    if preview_url and module_image_service.image_url_available(preview_url):
        suggestion["preview_url"] = preview_url
    if source_event_id:
        suggestion["source_event_id"] = source_event_id
    suggestions.append(suggestion)
    # 私有队列是工作区而不是历史，限制长度避免异常模组/长局无限增长。
    state["image_suggestions"] = suggestions[-100:]
    game_session.kp_state = state
    db.commit()
    db.refresh(game_session)
    return suggestion


def remove_image_suggestion(
    db: Session, game_session: GameSession, suggestion_key: str,
) -> None:
    key = suggestion_key.strip()
    if not key:
        return
    state = _private_state(game_session)
    remaining = [
        dict(item) for item in (state.get("image_suggestions") or [])
        if isinstance(item, dict) and str(item.get("key") or "") != key
    ]
    if len(remaining) == len(state.get("image_suggestions") or []):
        return
    state["image_suggestions"] = remaining
    game_session.kp_state = state
    db.commit()


def workspace_payload(
    db: Session, session_id: str, game_session: GameSession, module: Module,
) -> dict:
    try:
        player, others = _party(db, session_id, game_session)
    except ValueError as error:
        if str(error) != "会话缺少主角角色":
            raise
        player = None
        others = session_service.get_party_members(db, session_id)
    events = session_service.get_session_events(db, session_id)
    signals = director_signals.compute_signals(
        events, module, game_session.world_state or {},
        [*( [player.name] if player else []), *(char.name for char in others)],
    )
    state = _private_state(game_session)
    return {
        "notes": str(state.get("notes") or ""),
        "auto_ai_teammates": bool(state.get("auto_ai_teammates", False)),
        "image_suggestions": [
            dict(item) for item in (state.get("image_suggestions") or [])
            if isinstance(item, dict) and item.get("key") and item.get("prompt")
        ],
        "has_ai_teammates": bool(session_service.get_ai_teammates(db, session_id)),
        "has_unprocessed_player_turn": has_unprocessed_player_turn(db, session_id, game_session),
        "player_missing": player is None,
        "signals": asdict(signals),
        "catalogs": {
            "characters": [
                {"id": str(char.id), "name": str(char.name)}
                for char in ([player] if player else []) + others
            ],
            "scenes": [
                {"id": str(item.get("id") or ""), "name": str(item.get("title") or item.get("name") or item.get("id") or "")}
                for item in (module.scenes or []) if isinstance(item, dict)
            ],
            "npcs": [
                {
                    "id": str(item.get("id") or item.get("name") or ""),
                    "name": str(item.get("name") or item.get("id") or ""),
                }
                for item in (module.npcs or []) if isinstance(item, dict)
            ],
            "handouts": [
                {"id": str(item.get("id") or ""), "name": str(item.get("title") or item.get("id") or "")}
                for item in (getattr(module, "handouts", None) or []) if isinstance(item, dict)
            ],
        },
    }


def module_source_payload(db: Session, module: Module) -> dict:
    """返回 KP 专属的模组原文与解析结果，避免暴露给普通玩家接口。"""
    chunks = (
        db.query(ModuleChunk.ordinal, ModuleChunk.scene_hint, ModuleChunk.text)
        .filter(ModuleChunk.module_id == module.id)
        .order_by(ModuleChunk.ordinal.asc())
        .all()
    )
    return {
        "id": module.id,
        "title": module.title,
        "description": module.description or "",
        "raw_content": module.raw_content or "",
        "world_setting": module.world_setting or {},
        "truth": module.truth or "",
        "scenes": module.scenes or [],
        "npcs": module.npcs or [],
        "clues": module.clues or [],
        "triggers": module.triggers or [],
        "handouts": module.handouts or [],
        "maps": module.maps or [],
        "rag_status": module.rag_status or "",
        "chunks": [
            {
                "ordinal": ordinal,
                "scene_hint": scene_hint,
                "text": text,
            }
            for ordinal, scene_hint, text in chunks
        ],
    }


def update_workspace(
    db: Session,
    game_session: GameSession,
    *,
    notes: str | None = None,
    auto_ai_teammates: bool | None = None,
) -> dict:
    state = _private_state(game_session)
    if notes is not None:
        state["notes"] = notes.strip()
    if auto_ai_teammates is not None:
        state["auto_ai_teammates"] = bool(auto_ai_teammates)
    game_session.kp_state = state
    db.commit()
    db.refresh(game_session)
    return state


def auto_ai_teammates_enabled(game_session: GameSession) -> bool:
    return bool(_private_state(game_session).get("auto_ai_teammates", False))


def _advisor_context(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    query: str,
) -> list[dict]:
    player, others = _party(db, session_id, game_session)
    events = session_service.get_session_events(db, session_id)
    module_hits = (
        module_rag_service.retrieve(
            db, module.id, query, k=3, scene_id=game_session.current_scene_id,
        )
        if query.strip() and getattr(module, "rag_status", "") == "ready"
        else None
    )
    rule_hits = (
        rulebook_service.retrieve(db, query, module.rule_system, k=3)
        if query.strip() and rulebook_service.has_rulebook(db, module.rule_system)
        else None
    )
    return build_kp_context(
        game_session, module, player, events, teammates=others,
        rules_lookup_enabled=rulebook_service.has_rulebook(db, module.rule_system),
        module_excerpts=module_hits,
        module_lookup_enabled=getattr(module, "rag_status", "") == "ready",
        rule_excerpts=rule_hits,
    )


async def generate_narration_draft(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    instruction: str,
) -> str:
    instruction = instruction.strip() or "承接玩家最近行动，给出一段可编辑的场景回应。"
    messages = _advisor_context(db, session_id, game_session, module, instruction)
    messages.extend([
        {
            "role": "system",
            "content": (
                "你现在是真人 KP 的私有参谋，只写一份候选叙事草稿。不得调用工具、不得输出方括号控制标签，"
                "不得声称内容已经发生；草稿将在真人 KP 编辑并明确发布后才对玩家生效。"
            ),
        },
        {"role": "user", "content": f"真人 KP 的写作要求：{instruction}"},
    ])
    raw = await get_llm().complete(messages, temperature=0.7)
    draft = _CONTROL_TAG_RE.sub("", str(raw or "")).strip()
    if not draft:
        raise ValueError("AI 参谋没有返回可用草稿")
    return draft[:8000]


async def generate_turn_plan(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    focus: str,
) -> dict:
    player, others = _party(db, session_id, game_session)
    events = session_service.get_session_events(db, session_id)
    rules_enabled = rulebook_service.has_rulebook(db, module.rule_system)
    rule_hits = (
        rulebook_service.retrieve(db, focus, module.rule_system, k=3)
        if focus.strip() and rules_enabled else None
    )
    messages = turn_planner.build_turn_plan_messages(
        game_session, module, player, events, teammates=others,
        rules_lookup_enabled=rules_enabled, rule_excerpts=rule_hits,
    )
    if focus.strip():
        messages.append({
            "role": "user",
            "content": f"真人 KP 希望本次建议重点考虑：{focus.strip()}。仍只输出原 schema 的 JSON。",
        })
    plan = await turn_planner.run_turn_planner(get_fast_llm(), messages)
    if plan is None:
        raise ValueError("AI 参谋未能生成有效裁定建议")
    return plan.model_dump(mode="json")


def lookup(
    db: Session,
    game_session: GameSession,
    module: Module,
    scope: str,
    query: str,
) -> list[dict]:
    query = query.strip()
    if not query:
        raise ValueError("检索关键词不能为空")
    if scope == "rule":
        return rulebook_service.retrieve(db, query, module.rule_system, k=5)
    if scope == "module":
        return module_rag_service.retrieve(
            db, module.id, query, k=5, scene_id=game_session.current_scene_id,
        )
    raise ValueError("检索范围必须是 rule 或 module")


async def generate_image_preview(prompt: str, title: str) -> dict:
    image_llm = get_llm()
    if not image_llm.supports_image_gen():
        raise ValueError("当前 AI 配置不支持生图")
    translated = await get_fast_llm().complete(
        [
            {
                "role": "system",
                "content": "把真人 KP 的画面描述改写成一行英文文生图提示词。保留主体、环境、光线与年代，不要解释。",
            },
            {"role": "user", "content": prompt.strip()},
        ],
        temperature=0.4,
    )
    visual_prompt = str(translated or prompt).strip().splitlines()[0][:700]
    b64 = await image_llm.generate_image(f"{visual_prompt}, {_IMAGE_STYLE}")
    url = image_store.save_image_b64(b64 or "")
    if not url:
        raise ValueError("图片生成失败")
    return {"url": url, "title": title.strip() or "KP 配图"}


def publish_image(
    db: Session,
    session_id: str,
    url: str,
    title: str,
    suggestion_key: str = "",
):
    if not module_image_service.image_url_available(url):
        raise ValueError("预览图片不存在或已失效")
    game_session = db.get(GameSession, session_id)
    suggestion = None
    if game_session is not None and suggestion_key:
        suggestion = next(
            (
                item for item in (game_session.kp_state or {}).get("image_suggestions") or []
                if isinstance(item, dict) and str(item.get("key") or "") == suggestion_key.strip()
            ),
            None,
        )
    meta = {
        "kind": "illustration",
        "icat": "custom",
        "title": title.strip() or "KP 配图",
        "image": url,
        "kp_manual": True,
    }
    if suggestion:
        meta.update({
            "image_kind": suggestion.get("image_kind") or "",
            "image_item_id": suggestion.get("image_item_id") or "",
            "image_field": suggestion.get("image_field") or "",
            "kp_suggestion_key": suggestion_key.strip(),
        })
    event = session_service.add_event(
        db, session_id, "system", "—— KP 配图 ——",
        actor_name="KP", metadata=meta,
    )
    if suggestion and game_session is not None:
        module = db.get(Module, game_session.module_id)
        if module is not None:
            _write_image_cache(module, suggestion, url)
            db.add(module)
            db.commit()
        remove_image_suggestion(db, game_session, suggestion_key)
    return event


def _write_image_cache(module: Module, suggestion: dict, url: str) -> None:
    """把审核通过的自动配图回写模组 JSON，避免同一素材再次排队。"""
    kind = str(suggestion.get("image_kind") or "")
    item_id = str(suggestion.get("image_item_id") or "")
    field = str(suggestion.get("image_field") or "")
    list_field = {"scene": "scenes", "clue": "clues", "npc": "npcs", "handout": "handouts"}.get(kind)
    if not list_field or not item_id or not field:
        return
    items = [dict(item) if isinstance(item, dict) else item for item in (getattr(module, list_field, None) or [])]
    for item in items:
        if not isinstance(item, dict) or str(item.get("id") or "") != item_id:
            continue
        if field == "image_variant":
            variants = dict(item.get("image_variants") or {})
            variant_key = str(suggestion.get("variant_key") or "")
            if variant_key:
                variants[variant_key] = url
                item["image_variants"] = variants
        else:
            item[field] = url
        break
    setattr(module, list_field, items)


def current_player_turn_marker(
    db: Session, session_id: str,
) -> int:
    """AI 队友回合的推进信号（单调序号，与 last_ai_team_turn_seq 比较判「有无新回合」）。

    有真人玩家：以真人最新的行动/发言为信号（一批真人行动 → 一轮队友行动）。
    全 AI 玩家席（真人 KP 独走局）：以 KP 最新旁白为信号——KP 每发布一段叙事，
    即可推进一轮 AI 队友行动；同一段旁白不会重复推进。
    """
    human_ids = session_service.human_character_ids(db, session_id)
    events = session_service.get_session_events(db, session_id)
    if human_ids:
        return max(
            (
                event.sequence_num
                for event in events
                if event.actor_id in human_ids and event.event_type in ("action", "dialogue")
                and not (event.metadata_ or {}).get("pending_turn")
            ),
            default=0,
        )
    return max(
        (event.sequence_num for event in events if event.event_type == "narration"),
        default=0,
    )


def has_unprocessed_player_turn(
    db: Session, session_id: str, game_session: GameSession,
) -> bool:
    marker = current_player_turn_marker(db, session_id)
    last = int(_private_state(game_session).get("last_ai_team_turn_seq") or 0)
    return marker > last


async def run_human_team_turn_generation(session_id: str) -> None:
    """只运行 AI 队友的一轮公开行动，不生成 AI KP 叙事。"""
    from app.database import SessionLocal
    from app.services.event_protocol import make_chunk as _make_chunk
    from app.services.team_turn_service import _run_team_turn

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        if game_session is None or game_session.kp_mode != "human":
            return
        module = db.get(Module, game_session.module_id)
        player, _others = _party(db, session_id, game_session)
        teammates = session_service.get_ai_teammates(db, session_id)
        marker = current_player_turn_marker(db, session_id)
        if module is not None and teammates and has_unprocessed_player_turn(db, session_id, game_session):
            async for chunk in _run_team_turn(
                db, session_id, game_session, module, player, teammates, get_fast_llm(),
            ):
                room_hub.broadcast(session_id, chunk)
            state = _private_state(game_session)
            state["last_ai_team_turn_seq"] = marker
            game_session.kp_state = state
            db.commit()
        room_hub.broadcast(
            session_id,
            _make_chunk("kp_turn_ready", "玩家与 AI 队友回合已提交，等待真人 KP 处理"),
        )
        room_hub.broadcast(session_id, _make_chunk("done"))
    except Exception:
        logger.exception("真人 KP 的 AI 队友回合失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "AI 队友行动失败，请稍后重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()
