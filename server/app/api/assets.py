import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.asset import Asset
from app.schemas.asset import AssetRead

router = APIRouter(prefix="/api/assets", tags=["assets"])

# 素材库支持的类型（前端按类下拉；kind 字段本身不枚举、可扩，这里只作上传校验的白名单提示）
KINDS = {"floor", "wall", "door", "water", "rubble", "furniture", "item", "npc", "enemy", "player", "feature"}
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
MAX_BYTES = 4 * 1024 * 1024  # 单素材 4MB 上限


@router.get("", response_model=list[AssetRead])
def list_assets(kind: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Asset)
    if kind:
        q = q.filter(Asset.kind == kind)
    return q.order_by(Asset.created_at.desc()).all()


@router.post("", response_model=AssetRead)
async def upload_asset(
    file: UploadFile = File(...),
    name: str = Form(""),
    kind: str = Form("furniture"),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    """上传一件自定义素材（独立图片）。kind 见 KINDS；tags 逗号分隔。"""
    if file.content_type not in _EXT:
        raise HTTPException(400, f"不支持的图片类型：{file.content_type}（仅 png/jpg/webp/gif）")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "图片过大（上限 4MB）")
    if not data:
        raise HTTPException(400, "空文件")

    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    asset_id = str(uuid.uuid4())
    filename = f"{asset_id}{_EXT[file.content_type]}"
    (settings.assets_dir / filename).write_bytes(data)

    asset = Asset(
        id=asset_id,
        name=name.strip() or (file.filename or "未命名素材"),
        kind=kind.strip() or "furniture",
        filename=filename,
        mime=file.content_type,
        tags=[t.strip() for t in tags.replace("，", ",").split(",") if t.strip()],
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@router.get("/{asset_id}/image")
def asset_image(asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    path = settings.assets_dir / asset.filename
    if not path.exists():
        raise HTTPException(404, "素材文件丢失")
    return FileResponse(path, media_type=asset.mime)


@router.delete("/{asset_id}")
def delete_asset(asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    if asset.builtin:
        raise HTTPException(400, "内置素材不可删除")
    (settings.assets_dir / asset.filename).unlink(missing_ok=True)
    db.delete(asset)
    db.commit()
    return {"ok": True}
