"""场景、线索、遭遇、手书与 NPC 立绘的异步生成和缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from sqlalchemy.orm import Session

from app.ai.context import _active_flags, _resolve_state
from app.ai.llm_factory import get_fast_llm, get_llm
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.services import human_kp_service, module_image_service, session_service, turn_context
from app.services.event_protocol import make_chunk
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)
_make_chunk = make_chunk
_scene_name = turn_context._scene_name
_apply_world_memory = turn_context._apply_world_memory

# 全部配图的统一画风后缀（确定性追加在快模型产出的提示词之后，保证风格一致）：
# 非全彩漫画——墨线 + 网点/排线阴影，单色为主、少量低饱和点缀色，阴郁美漫质感。
_ILLUST_STYLE_SUFFIX = (
    "monochrome manga illustration, bold ink lineart, cross-hatching and screentone shading, "
    "mostly black and white with sparse desaturated color accent, gritty dark comic style"
)

_HANDOUT_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 手书（信件/报纸/日记/便条）转成一行**英文**"
    " Stable Diffusion 提示词：只描绘这份实体文书本身的画面内容——纸张、字迹/排版、年代感、"
    "光线氛围（如 aged paper, ink handwriting, dim candlelight）。画风词不用写，系统会统一追加。"
    "不要出现人物面孔与真实人名，不要引号，只输出提示词本身。"
)

async def _illustrate_event(
    session_id: str,
    event_id: str,
    prompt_sys: str,
    prompt_user: str,
    cache_write=None,
    patch_key: str = "image",
) -> None:
    """通用配图管线（后台）：快模型写英文提示词 → 图片后端出图 → 落盘 →
    补挂事件 metadata[patch_key] 并广播 event_patch 增量。

    ``cache_write(url)``：生成成功后回写缓存（如模组 scenes[].image / npcs[].portrait），
    同一素材下次直接秒出、不再重复烧卡；回写失败只弃缓存，不影响本次补挂。
    任何环节失败一律静默放弃（卡片保持纯文字），绝不影响跑团主流程。
    """
    from app.database import SessionLocal
    from app.services.image_store import save_image_b64

    try:
        raw = await get_fast_llm().complete(
            [
                {"role": "system", "content": prompt_sys},
                {"role": "user", "content": prompt_user},
            ],
            temperature=0.7,
        )
        sd_prompt = (raw or "").strip().splitlines()[0].strip()[:500] if isinstance(raw, str) and raw.strip() else ""
        if not sd_prompt:
            return
        sd_prompt = f"{sd_prompt}, {_ILLUST_STYLE_SUFFIX}"
        b64 = await get_llm().generate_image(sd_prompt)
        if not b64:
            return
        url = save_image_b64(b64)
        if not url:
            return
        if cache_write is not None:
            try:
                cache_write(url)
            except Exception:  # noqa: BLE001 — 缓存是增强件，回写失败不影响本次出图
                logger.exception("配图缓存回写失败（本次仍补挂事件）：event=%s", event_id)
        db = SessionLocal()
        try:
            ev = db.get(EventLog, event_id)
            if ev is None:
                return
            meta = dict(ev.metadata_ or {})
            meta[patch_key] = url
            ev.metadata_ = meta
            db.commit()
        finally:
            db.close()
        room_hub.broadcast(session_id, _make_chunk(
            "event_patch", metadata={"event_id": event_id, "patch": {patch_key: url}},
        ))
        logger.info("配图完成：event=%s key=%s url=%s", event_id, patch_key, url)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("配图失败（卡片保持纯文字）：event=%s", event_id)


async def _illustrate_handout(
    session_id: str, event_id: str, title: str, kind: str, content: str,
) -> None:
    """手书配图：通用管线的薄封装（保持既有调用方与测试的签名/行为不变）。"""
    await _illustrate_event(
        session_id, event_id, _HANDOUT_PROMPT_SYS,
        f"标题：{title}\n类型：{kind}\n正文：\n{content[:600]}",
    )


def _spawn_illustration(
    session_id: str,
    event_id: str,
    prompt_sys: str,
    prompt_user: str,
    cache_write=None,
    patch_key: str = "image",
    on_done=None,
) -> bool:
    """照 ``_exec_handout`` 的模式起后台配图任务（fail-open）：先判定生图能力、整体 try/except，
    无能力或起任务失败都静默返回 False（卡片保持纯文字），绝不阻塞主流程。

    ``on_done``：任务收尾（无论成败）时调用——供调用方清理「生成中」防重标记；
    没能起任务时也会调用，保证标记不残留。
    """
    spawned = False
    try:
        if get_llm().supports_image_gen():

            async def _run() -> None:
                try:
                    await _illustrate_event(
                        session_id, event_id, prompt_sys, prompt_user,
                        cache_write=cache_write, patch_key=patch_key,
                    )
                finally:
                    if on_done is not None:
                        on_done()

            asyncio.create_task(_run())
            spawned = True
    except Exception:  # noqa: BLE001 — 配图判定/起任务失败不影响主流程
        logger.exception("配图任务启动失败（忽略）：event=%s", event_id)
    if not spawned and on_done is not None:
        on_done()
    return spawned


def _module_list_cache_writer(module_id: str, list_field: str, item_id: str, key: str):
    """返回一个缓存回写回调：把生成的图片 URL 写进模组 JSON 列表字段（scenes/npcs/clues）
    中指定 id 条目的 ``key`` 上。

    在后台任务里执行 → 用独立 DB 会话；JSON 列必须整列表重赋值才会被 SQLAlchemy 追踪落库。
    """
    def _write(url: str) -> None:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            m = db.get(Module, module_id)
            if m is None:
                return
            items = [dict(it) if isinstance(it, dict) else it for it in (getattr(m, list_field, None) or [])]
            for it in items:
                if isinstance(it, dict) and str(it.get("id") or "") == item_id:
                    it[key] = url
                    break
            setattr(m, list_field, items)
            db.commit()
        finally:
            db.close()
    return _write


def _scene_variant_cache_writer(module_id: str, scene_id: str, visual_state_key: str):
    """把场景状态图写入 image_variants，基础状态仍写 scenes[].image。"""
    def _write(url: str) -> None:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            module = db.get(Module, module_id)
            if module is None:
                return
            scenes = [dict(value) if isinstance(value, dict) else value for value in (module.scenes or [])]
            for scene in scenes:
                if not isinstance(scene, dict) or str(scene.get("id") or "") != scene_id:
                    continue
                if visual_state_key == "base":
                    scene["image"] = url
                else:
                    variants = dict(scene.get("image_variants") or {})
                    variants[visual_state_key] = url
                    scene["image_variants"] = variants
                break
            module.scenes = scenes
            db.commit()
        finally:
            db.close()
    return _write


def _module_era(module: Module) -> str:
    """模组年代标签（1920s/现代/维多利亚…），生图提示词素材；缺省沿用全站惯例 1920s。"""
    return str((module.world_setting or {}).get("era") or "1920s")


_SCENE_ILLUST_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 场景转成一行**英文** Stable Diffusion 提示词："
    "只描绘该地点的空镜画面内容——环境/建筑、光影、天气与年代质感，按给定年代取材"
    "（如 abandoned train car, flickering lights）。危险度越高画面越阴沉压抑。画风词不用写，系统会统一追加。"
    "不要出现人物面孔与真实人名，不要引号，只输出提示词本身。"
)

_SCENE_ILLUST_INFLIGHT: set[tuple[str, str]] = set()

_SCENE_VISUAL_FIELDS = (
    "title", "name", "description", "danger", "atmosphere", "map", "visual_variant", "visual_prompt",
)


def _scene_visual_state(
    module: Module, game_session: GameSession, scene_id: str,
) -> tuple[str, dict]:
    """返回稳定视觉状态键与按剧情 flag 解析后的场景。"""
    base = next(
        (s for s in (module.scenes or []) if isinstance(s, dict) and s.get("id") == scene_id),
        None,
    )
    if base is None:
        return "base", {}
    resolved = _resolve_state(base, _active_flags(game_session))
    explicit = str(resolved.get("visual_variant") or "").strip()
    base_payload = {key: base.get(key) for key in _SCENE_VISUAL_FIELDS}
    resolved_payload = {key: resolved.get(key) for key in _SCENE_VISUAL_FIELDS}
    if explicit:
        return explicit, resolved
    if base_payload == resolved_payload:
        return "base", resolved
    digest = hashlib.sha256(
        json.dumps(resolved_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"v1-{digest}", resolved


def _scene_card_key(scene_id: str, visual_state_key: str) -> str:
    return f"{scene_id}|{visual_state_key}"

_NPC_PORTRAIT_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG NPC 转成一行**英文** Stable Diffusion 提示词："
    "该人物的半身肖像（character portrait, bust shot，按给定年代取服饰）。据外貌/身份/性格"
    "描绘气质与神态。画风词不用写，系统会统一追加。不要出现真实人名，"
    "不要引号，只输出提示词本身。"
)

_CLUE_ILLUST_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 线索转成一行**英文** Stable Diffusion 提示词："
    "描绘这件线索物证本身的特写画面——材质、细节、陈放环境与年代质感（evidence close-up, "
    "dim lighting）。画风词不用写，系统会统一追加。不要出现人物面孔与真实人名，不要引号，只输出提示词本身。"
)

_ENCOUNTER_ILLUST_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 遭遇战敌人转成一行**英文** Stable Diffusion 提示词："
    "描绘紧张的遭遇场面（horror creature encounter, dramatic composition），按敌方"
    "描述刻画其形貌与压迫感，按给定年代取环境质感。不要出现真实人名，不要引号，只输出提示词本身。"
)


def _maybe_scene_illustration(
    db: Session, session_id: str, module: Module, scene_id: str | None,
) -> list[str]:
    """按「场景 + 视觉状态」落配图卡；状态卡存在但图片失效时修复原卡，不重复落卡。"""
    try:
        if not scene_id:
            return []
        game_session = db.get(GameSession, session_id)
        if game_session is None:
            return []
        scene = next(
            (s for s in (module.scenes or []) if isinstance(s, dict) and s.get("id") == scene_id),
            None,
        )
        if scene is None:
            return []
        visual_state_key, resolved_scene = _scene_visual_state(module, game_session, scene_id)
        card_key = _scene_card_key(scene_id, visual_state_key)
        scene_cards = set((game_session.world_state or {}).get("scene_cards") or [])
        base_seen = scene_id in scene_cards  # 兼容旧会话只记录 scene_id 的基础卡
        seen = card_key in scene_cards or (visual_state_key == "base" and base_seen)
        variants = dict(scene.get("image_variants") or {})
        cached = str(
            scene.get("image") if visual_state_key == "base" else variants.get(visual_state_key)
            or ""
        ).strip()
        if not module_image_service.image_url_available(cached):
            cached = ""

        existing = next(
            (
                event for event in session_service.get_session_events(db, session_id)
                if (event.metadata_ or {}).get("kind") == "illustration"
                and (event.metadata_ or {}).get("icat") == "scene"
                and str((event.metadata_ or {}).get("image_item_id") or "") == scene_id
                and str((event.metadata_ or {}).get("visual_state_key") or ("base" if visual_state_key == "base" else "")) == visual_state_key
            ),
            None,
        )
        # 后台任务可能刚回写数据库，而当前请求仍持有旧 Module 对象；事件 metadata 是本卡的
        # 最新快照，命中有效图片时不要因对象陈旧再次启动一张相同状态图。
        if not cached and existing is not None:
            event_image = str((existing.metadata_ or {}).get("image") or "").strip()
            if module_image_service.image_url_available(event_image):
                cached = event_image
        title = str(resolved_scene.get("title") or resolved_scene.get("name") or "").strip() or scene_id
        prompt_user = (
            f"场景：{title}\n年代：{_module_era(module)}\n"
            f"危险度：{resolved_scene.get('danger') or ''}\n氛围：{resolved_scene.get('atmosphere') or ''}\n"
            f"描述：{str(resolved_scene.get('description') or '')[:600]}"
        )

        if game_session.kp_mode == "human":
            human_kp_service.queue_image_suggestion(
                db, game_session,
                key=f"scene:{scene_id}:{visual_state_key}",
                title=title,
                prompt=prompt_user,
                image_kind="scene",
                image_item_id=scene_id,
                image_field="image" if visual_state_key == "base" else "image_variant",
                variant_key=visual_state_key if visual_state_key != "base" else "",
                preview_url=cached,
                source_event_id=str(existing.id) if existing is not None else "",
            )
            return []

        def repair(event) -> None:
            if cached or event is None:
                return
            inflight_key = (session_id, card_key)
            if inflight_key in _SCENE_ILLUST_INFLIGHT:
                return
            _SCENE_ILLUST_INFLIGHT.add(inflight_key)
            _spawn_illustration(
                session_id, event.id, _SCENE_ILLUST_PROMPT_SYS, prompt_user,
                cache_write=_scene_variant_cache_writer(module.id, scene_id, visual_state_key),
                on_done=lambda _key=inflight_key: _SCENE_ILLUST_INFLIGHT.discard(_key),
            )

        if seen:
            repair(existing)
            return []

        meta: dict = {
            "kind": "illustration", "icat": "scene", "title": title,
            "image_kind": "scene", "image_item_id": scene_id,
            "image_field": "image" if visual_state_key == "base" else "image_variant",
            "visual_state_key": visual_state_key,
        }
        if cached:
            meta["image"] = cached
        ev = session_service.add_event(
            db, session_id, "system",
            f"—— 抵达 {title} ——" if visual_state_key == "base" else f"—— 场景状态变化：{title} ——",
            actor_name="系统", metadata=meta,
        )
        # 先记防重再起任务：即使生图失败，同一视觉状态也不重复出卡。
        _apply_world_memory(
            db, game_session,
            lambda ws, _key=card_key: {
                **ws, "scene_cards": [*(ws.get("scene_cards") or []), _key],
            },
        )
        if not cached:
            repair(ev)
        return [_make_chunk("system", ev.content, metadata=ev.metadata_, event_id=ev.id)]
    except Exception:  # noqa: BLE001 — 配图卡是增强件，任何失败都不许影响场景切换
        logger.exception("场景配图卡落卡失败（忽略）：session=%s scene=%s", session_id, scene_id)
        return []


def _maybe_clue_illustration(
    db: Session, session_id: str, module: Module, clue_id: str,
) -> None:
    """线索发现配图卡：某线索**首次**进台账时落一条 illustration 系统事件并直接广播
    （调用点在世界记忆钩子里，不在 chunk 流水线上，故直接 room_hub.broadcast）。

    模组 ``clues[].image`` 有缓存秒出，否则后台生图并回写缓存。匹配不到模组线索
    （素材缺失）则不出卡。整体 fail-open。
    """
    try:
        clue = next(
            (
                c for c in (module.clues or [])
                if isinstance(c, dict) and str(c.get("id") or "") == clue_id
            ),
            None,
        )
        if clue is None:
            return
        name = str(clue.get("name") or "").strip() or clue_id
        meta: dict = {
            "kind": "illustration", "icat": "clue", "title": name,
            "image_kind": "clue", "image_item_id": clue_id, "image_field": "image",
        }
        cached = str(clue.get("image") or "").strip()
        if not module_image_service.image_url_available(cached):
            cached = ""
        if cached:
            meta["image"] = cached
        game_session = db.get(GameSession, session_id)
        if game_session is not None and game_session.kp_mode == "human":
            human_kp_service.queue_image_suggestion(
                db, game_session,
                key=f"clue:{module.id}:{clue_id}",
                title=name,
                prompt=(
                    f"线索：{name}\n年代：{_module_era(module)}\n"
                    f"内容：{str(clue.get('description') or '')[:600]}"
                ),
                image_kind="clue",
                image_item_id=clue_id,
                image_field="image",
                preview_url=cached,
            )
            return
        ev = session_service.add_event(
            db, session_id, "system", f"—— 发现线索：{name} ——", actor_name="系统", metadata=meta,
        )
        room_hub.broadcast(
            session_id, _make_chunk("system", ev.content, metadata=ev.metadata_, event_id=ev.id),
        )
        if not cached:
            _spawn_illustration(
                session_id, ev.id, _CLUE_ILLUST_PROMPT_SYS,
                (
                    f"线索：{name}\n年代：{_module_era(module)}\n"
                    f"内容：{str(clue.get('description') or '')[:600]}"
                ),
                cache_write=_module_list_cache_writer(module.id, "clues", clue_id, "image"),
            )
    except Exception:  # noqa: BLE001 — 配图卡是增强件，任何失败都不许影响线索记账
        logger.exception("线索配图卡落卡失败（忽略）：session=%s clue=%s", session_id, clue_id)


def _maybe_encounter_illustration(
    db: Session, session_id: str, module: Module, enemies: list[dict],
) -> list[str]:
    """遭遇战配图卡：结构化开战时落一条 illustration 系统事件，返回待广播 chunks。

    首个能匹配到模组 NPC 的敌人若带 ``encounter_image`` 缓存则秒出并不再生图；
    否则后台生图，成功回写该 NPC 的 ``encounter_image``（临场杂兵无模组条目，不回写）。
    """
    try:
        names = [str(e.get("name") or "").strip() for e in (enemies or []) if e.get("name")]
        if not names:
            return []
        # 敌方里首个模组正牌 NPC：它的缓存与回写位（杂兵没有归宿，只借它的档案存图）
        npc_ids = {str(n.get("id") or "") for n in (module.npcs or []) if isinstance(n, dict)}
        anchor = next(
            (e for e in enemies if str(e.get("id") or "") in npc_ids and e.get("id")), None,
        )
        meta: dict = {
            "kind": "illustration", "icat": "encounter", "title": "遭遇战",
            "image_kind": "npc" if anchor is not None else "",
            "image_item_id": str(anchor.get("id")) if anchor is not None else "",
            "image_field": "encounter_image" if anchor is not None else "",
        }
        cached = str((anchor or {}).get("encounter_image") or "").strip()
        if not module_image_service.image_url_available(cached):
            cached = ""
        if cached:
            meta["image"] = cached
        game_session = db.get(GameSession, session_id)
        if game_session is not None and game_session.kp_mode == "human":
            desc = "；".join(
                f"{str(e.get('name') or '')}：{str(e.get('description') or '')[:200]}"
                for e in enemies if e.get("name")
            )
            human_kp_service.queue_image_suggestion(
                db, game_session,
                key=(
                    f"encounter:{module.id}:"
                    f"{','.join(str(e.get('id') or e.get('name') or '') for e in enemies)}"
                ),
                title="遭遇战",
                prompt=f"敌方：{desc}\n年代：{_module_era(module)}",
                image_kind="npc" if anchor is not None else "",
                image_item_id=str(anchor.get("id")) if anchor is not None else "",
                image_field="encounter_image" if anchor is not None else "",
                preview_url=cached,
            )
            return []
        ev = session_service.add_event(
            db, session_id, "system", f"—— 遭遇：{'、'.join(names)} ——",
            actor_name="系统", metadata=meta,
        )
        if not cached:
            desc = "；".join(
                f"{str(e.get('name') or '')}：{str(e.get('description') or '')[:200]}"
                for e in enemies if e.get("name")
            )
            _spawn_illustration(
                session_id, ev.id, _ENCOUNTER_ILLUST_PROMPT_SYS,
                f"敌方：{desc}\n年代：{_module_era(module)}",
                cache_write=(
                    _module_list_cache_writer(
                        module.id, "npcs", str(anchor.get("id")), "encounter_image",
                    ) if anchor is not None else None
                ),
            )
        return [_make_chunk("system", ev.content, metadata=ev.metadata_, event_id=ev.id)]
    except Exception:  # noqa: BLE001 — 配图卡是增强件，任何失败都不许影响开战
        logger.exception("遭遇配图卡落卡失败（忽略）：session=%s", session_id)
        return []


# NPC 立绘「生成中」防重：同一 (module_id, npc_id) 同时只跑一张（进程级，会话间共享缓存目标）。
_PORTRAIT_INFLIGHT: set[tuple[str, str]] = set()


def _attach_npc_portrait(db: Session, session_id: str, module: Module, ev) -> None:
    """NPC 立绘钩子：一条 NPC 对话事件落库后，说话人匹配到模组 NPC 时——
    有 ``portrait`` 缓存 → 事件 metadata 直接补 portrait（并广播 event_patch，live 侧即时上头像）；
    无缓存且未在生成中 → 起后台生图，成功回写 ``module.npcs[].portrait`` 并 patch 该事件。

    整体 fail-open：这是纯装饰增强，任何失败都不许影响对话落库。
    """
    try:
        if getattr(ev, "event_type", "") != "dialogue":
            return
        name = (getattr(ev, "actor_name", "") or "").strip()
        actor_id = getattr(ev, "actor_id", None)
        npc = next(
            (
                n for n in (module.npcs or [])
                if isinstance(n, dict)
                and (n.get("name") == name or n.get("id") == name or (actor_id and n.get("id") == actor_id))
            ),
            None,
        )
        if npc is None:
            return
        cached = str(npc.get("portrait") or "").strip()
        if not module_image_service.image_url_available(cached):
            cached = ""
        game_session = db.get(GameSession, session_id)
        if game_session is not None and game_session.kp_mode == "human":
            human_kp_service.queue_image_suggestion(
                db, game_session,
                key=f"npc-portrait:{module.id}:{npc.get('id') or name}",
                title=f"{npc.get('name') or name} 立绘",
                prompt=(
                    f"NPC：{npc.get('name') or name}\n年代：{_module_era(module)}\n"
                    f"外貌与身份：{str(npc.get('description') or '')[:400]}\n"
                    f"性格：{str(npc.get('personality') or '')[:200]}"
                ),
                image_kind="npc",
                image_item_id=str(npc.get("id") or ""),
                image_field="portrait",
                preview_url=cached,
                source_event_id=str(getattr(ev, "id", "") or ""),
            )
            return
        if cached:
            meta = dict(ev.metadata_ or {})
            if meta.get("portrait") == cached:
                return
            meta["portrait"] = cached
            meta.update({"image_kind": "npc", "image_item_id": str(npc.get("id") or ""), "image_field": "portrait"})
            ev.metadata_ = meta
            db.add(ev)
            db.commit()
            room_hub.broadcast(session_id, _make_chunk(
                "event_patch", metadata={"event_id": ev.id, "patch": {"portrait": cached}},
            ))
            return
        key = (str(module.id), str(npc.get("id") or name))
        if key in _PORTRAIT_INFLIGHT:
            return
        meta = dict(ev.metadata_ or {})
        meta.update({
            "image_kind": "npc",
            "image_item_id": str(npc.get("id") or ""),
            "image_field": "portrait",
        })
        ev.metadata_ = meta
        db.add(ev)
        db.commit()
        _PORTRAIT_INFLIGHT.add(key)
        _spawn_illustration(
            session_id, ev.id, _NPC_PORTRAIT_PROMPT_SYS,
            (
                f"NPC：{npc.get('name') or name}\n年代：{_module_era(module)}\n"
                f"外貌与身份：{str(npc.get('description') or '')[:400]}\n"
                f"性格：{str(npc.get('personality') or '')[:200]}"
            ),
            cache_write=(
                _module_list_cache_writer(module.id, "npcs", str(npc.get("id")), "portrait")
                if npc.get("id") else None
            ),
            patch_key="portrait",
            on_done=lambda _key=key: _PORTRAIT_INFLIGHT.discard(_key),
        )
    except Exception:  # noqa: BLE001
        logger.exception("NPC 立绘钩子失败（忽略）：session=%s actor=%s", session_id, getattr(ev, "actor_name", "?"))


def _attach_npc_portraits(db: Session, session_id: str, evs: list) -> None:
    """批量版立绘钩子：供 ``_persist_narration`` 这类拿不到 module 的落库收尾处调用，
    只在确有对话事件时才查一次模组。"""
    if not evs:
        return
    try:
        gs = db.get(GameSession, session_id)
        module = db.get(Module, gs.module_id) if gs else None
    except Exception:  # noqa: BLE001
        return
    if module is None:
        return
    for ev in evs:
        _attach_npc_portrait(db, session_id, module, ev)
