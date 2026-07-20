"""模组结构化条目的配图生成与缓存修复。"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.ai.llm_factory import get_fast_llm, get_llm
from app.models.module import Module
from app.services import image_store

logger = logging.getLogger(__name__)

_IMAGE_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(?:jpg|jpeg|png|webp)$")
_STYLE_SUFFIX = (
    "monochrome manga illustration, bold ink lineart, cross-hatching and screentone shading, "
    "mostly black and white with sparse desaturated color accent, gritty dark comic style"
)

SCENE_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 场景转成一行**英文** Stable Diffusion 提示词："
    "只描绘该地点的空镜画面内容——环境/建筑、光影、天气与年代质感，按给定年代取材"
    "（如 abandoned train car, flickering lights）。危险度越高画面越阴沉压抑。画风词不用写，系统会统一追加。"
    "不要出现人物面孔与真实人名，不要引号，只输出提示词本身。"
)

NPC_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG NPC 转成一行**英文** Stable Diffusion 提示词："
    "该人物的半身肖像（character portrait, bust shot，按给定年代取服饰）。据外貌/身份/性格"
    "描绘气质与神态。画风词不用写，系统会统一追加。不要出现真实人名，不要引号，只输出提示词本身。"
)

ENCOUNTER_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 遭遇战敌人转成一行**英文** Stable Diffusion 提示词："
    "描绘紧张的遭遇场面（horror creature encounter, dramatic composition），按敌方"
    "描述刻画其形貌与压迫感，按给定年代取环境质感。不要出现真实人名，不要引号，只输出提示词本身。"
)

CLUE_PROMPT_SYS = (
    "你是文生图提示词工程师。把给定的 TRPG 线索转成一行**英文** Stable Diffusion 提示词："
    "描绘这件线索物证本身的特写画面——材质、细节、陈放环境与年代质感（evidence close-up, "
    "dim lighting）。画风词不用写，系统会统一追加。不要出现人物面孔与真实人名，不要引号，只输出提示词本身。"
)

_TARGETS = {
    "scene": ("scenes", "image", SCENE_PROMPT_SYS),
    "npc": ("npcs", "portrait", NPC_PROMPT_SYS),
    "clue": ("clues", "image", CLUE_PROMPT_SYS),
}


def _target(module: Module, kind: str, item_id: str, field: str | None = None) -> tuple[dict, str, str, str]:
    config = _TARGETS.get(kind)
    if config is None:
        raise ValueError("不支持的图片类型")
    list_field, expected_field, prompt_sys = config
    allowed_fields = ("portrait", "encounter_image") if kind == "npc" else (expected_field,)
    if field and field not in allowed_fields:
        raise ValueError("图片字段与类型不匹配")
    target_field = field or expected_field
    for item in getattr(module, list_field, None) or []:
        if isinstance(item, dict) and str(item.get("id") or "") == str(item_id):
            return item, list_field, target_field, (
                ENCOUNTER_PROMPT_SYS if target_field == "encounter_image" else prompt_sys
            )
    raise LookupError("模组图片条目不存在")


def image_url_available(url: str | None) -> bool:
    """只把本地图片 URL 且对应文件存在视为可复用缓存。"""
    value = str(url or "").strip()
    if not value.startswith("/api/images/"):
        return False
    name = value.rsplit("/", 1)[-1]
    return bool(_IMAGE_NAME_RE.fullmatch(name)) and (image_store.IMAGES_DIR / name).is_file()


def _prompt_user(kind: str, item: dict, module: Module, field: str) -> str:
    era = str((module.world_setting or {}).get("era") or "1920s")
    if kind == "scene":
        return (
            f"场景：{item.get('title') or item.get('name') or item.get('id') or ''}\n"
            f"年代：{era}\n危险度：{item.get('danger') or ''}\n"
            f"氛围：{item.get('atmosphere') or ''}\n"
            f"描述：{str(item.get('description') or '')[:600]}"
        )
    if kind == "npc":
        if field == "encounter_image":
            return (
                f"敌方：{item.get('name') or item.get('id') or ''}\n年代：{era}\n"
                f"形貌与能力：{str(item.get('description') or '')[:400]}\n"
                f"武器/攻击方式：{str(item.get('weapon') or '')[:200]}"
            )
        return (
            f"NPC：{item.get('name') or item.get('id') or ''}\n年代：{era}\n"
            f"外貌与身份：{str(item.get('description') or '')[:400]}\n"
            f"性格：{str(item.get('personality') or '')[:200]}"
        )
    return (
        f"线索：{item.get('name') or item.get('id') or ''}\n年代：{era}\n"
        f"内容：{str(item.get('description') or '')[:600]}"
    )


async def regenerate_module_image(
    db: Session,
    module: Module,
    kind: str,
    item_id: str,
    field: str | None = None,
) -> str | None:
    """重新生成一个失效的模组图片，并将新 URL 原子地写回模组 JSON。"""
    item, list_field, expected_field, prompt_sys = _target(module, kind, item_id, field)
    cached = str(item.get(expected_field) or "").strip()
    if image_url_available(cached):
        return cached

    image_llm = get_llm()
    if not image_llm.supports_image_gen():
        return None
    try:
        raw = await get_fast_llm().complete(
            [
                {"role": "system", "content": prompt_sys},
                {"role": "user", "content": _prompt_user(kind, item, module, expected_field)},
            ],
            temperature=0.7,
        )
        prompt = (raw or "").strip().splitlines()[0].strip()[:500] if raw else ""
        if not prompt:
            return None
        b64 = await image_llm.generate_image(f"{prompt}, {_STYLE_SUFFIX}")
        if not b64:
            return None
        url = image_store.save_image_b64(b64)
        if not url:
            return None

        items = [dict(value) if isinstance(value, dict) else value for value in (getattr(module, list_field, None) or [])]
        updated = False
        for value in items:
            if isinstance(value, dict) and str(value.get("id") or "") == str(item_id):
                value[expected_field] = url
                updated = True
                break
        if not updated:
            return None
        setattr(module, list_field, items)
        db.commit()
        db.refresh(module)
        return url
    except Exception:  # noqa: BLE001 — 图片是增强能力，失败时由调用方返回可读错误
        logger.exception("模组图片重新生成失败：module=%s kind=%s item=%s", module.id, kind, item_id)
        return None
