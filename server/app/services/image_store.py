"""生成图片的落盘存储：文件存数据目录 images/（与 trpg.db 同处），事件只存相对 URL。

不存 SQLite BLOB——图片会让库文件暴涨，且迁移前的整库自动备份会跟着膨胀。
入盘统一转 JPEG（质量 85）：1024² 约 200KB，体积只有 PNG 的约 1/7。
"""

from __future__ import annotations

import base64
import io
import logging
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

IMAGES_DIR = settings.db_path.parent / "images"


def save_image_b64(b64: str) -> str | None:
    """把 base64 图片转存为 JPEG 文件，返回相对 URL ``/api/images/{name}``；失败返回 None。"""
    try:
        from PIL import Image

        raw = base64.b64decode(b64)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.jpg"
        im.save(IMAGES_DIR / name, "JPEG", quality=85)
        return f"/api/images/{name}"
    except Exception:  # noqa: BLE001 — 存图失败只弃图，绝不上抛
        logger.exception("生成图片落盘失败（已弃图）")
        return None
