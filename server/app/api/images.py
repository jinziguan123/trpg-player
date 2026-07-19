"""生成图片的访问端点：只读、白名单文件名、严防路径穿越。"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.image_store import IMAGES_DIR

router = APIRouter(prefix="/api/images", tags=["images"])

# 落盘文件名恒为 uuid4.hex + 扩展名（见 image_store.save_image_b64），白名单校验即防穿越
_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(jpg|jpeg|png|webp)$")


@router.get("/{filename}")
def get_image(filename: str):
    if not _NAME_RE.fullmatch(filename):
        raise HTTPException(404, "图片不存在")
    path = IMAGES_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})
