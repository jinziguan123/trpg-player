import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.asset import Asset, AssetCategory
from app.schemas.asset import AssetRead, AssetUpdate, CategoryLabel, CategoryRead, CategoryWrite

router = APIRouter(prefix="/api/assets", tags=["assets"])
categories_router = APIRouter(prefix="/api/asset-categories", tags=["assets"])

# 内置类别（key, 中文标签）——系统渲染语义依赖这些 key（地形/token），不可删；自定义类别另存表。
BUILTIN_CATEGORIES = [
    ("floor", "地板"), ("wall", "墙"), ("door", "门"), ("water", "水"), ("rubble", "碎石"),
    ("furniture", "家具"), ("item", "物品"), ("npc", "NPC"), ("enemy", "敌人"), ("player", "玩家"), ("feature", "景物"),
]
_BUILTIN_KEYS = {k for k, _ in BUILTIN_CATEGORIES}
# 校验上传/编辑的 kind：内置 key + 已建自定义 key（动态，见 _valid_kinds）
KINDS = _BUILTIN_KEYS
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
MAX_BYTES = 4 * 1024 * 1024  # 单素材 4MB 上限


@router.get("", response_model=list[AssetRead])
def list_assets(kind: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Asset)
    if kind:
        q = q.filter(Asset.kind == kind)
    # 默认素材排在前：渲染器按类型取「第一个命中」即得默认（is_default 全局靠前，故每类默认者居前）。
    return q.order_by(Asset.is_default.desc(), Asset.created_at.desc()).all()


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


@router.post("/{asset_id}/default", response_model=AssetRead)
def set_default(asset_id: str, db: Session = Depends(get_db)):
    """把该素材设为其类型的默认（同类型其它素材取消默认）。"""
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    for other in db.query(Asset).filter(Asset.kind == asset.kind, Asset.is_default.is_(True)).all():
        other.is_default = False
    asset.is_default = True
    db.commit()
    db.refresh(asset)
    return asset


def _valid_kinds(db: Session) -> set[str]:
    return _BUILTIN_KEYS | {c.key for c in db.query(AssetCategory).all()}


@router.patch("/{asset_id}", response_model=AssetRead)
def update_asset(asset_id: str, data: AssetUpdate, db: Session = Depends(get_db)):
    """编辑素材：改名 / 改类别 / 改标签。"""
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    if data.name is not None:
        asset.name = data.name.strip() or asset.name
    if data.kind is not None:
        if data.kind not in _valid_kinds(db):
            raise HTTPException(400, f"未知类别：{data.kind}")
        asset.kind = data.kind
    if data.tags is not None:
        asset.tags = [t.strip() for t in data.tags if t.strip()]
    db.commit()
    db.refresh(asset)
    return asset


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


# ── 素材类别 CRUD（内置只读，自定义可增删改）──
@categories_router.get("", response_model=list[CategoryRead])
def list_categories(db: Session = Depends(get_db)):
    out = [CategoryRead(key=k, label=lbl, builtin=True) for k, lbl in BUILTIN_CATEGORIES]
    out += [CategoryRead(key=c.key, label=c.label, builtin=False) for c in db.query(AssetCategory).all()]
    return out


@categories_router.post("", response_model=CategoryRead)
def create_category(data: CategoryWrite, db: Session = Depends(get_db)):
    key = data.key.strip()
    if not key:
        raise HTTPException(400, "类别 key 不能为空")
    if key in _BUILTIN_KEYS or db.get(AssetCategory, key):
        raise HTTPException(400, f"类别已存在：{key}")
    cat = AssetCategory(key=key, label=data.label.strip() or key)
    db.add(cat)
    db.commit()
    return CategoryRead(key=cat.key, label=cat.label, builtin=False)


@categories_router.put("/{key}", response_model=CategoryRead)
def rename_category(key: str, data: CategoryLabel, db: Session = Depends(get_db)):
    if key in _BUILTIN_KEYS:
        raise HTTPException(400, "内置类别名称不可修改")
    cat = db.get(AssetCategory, key)
    if not cat:
        raise HTTPException(404, "类别不存在")
    cat.label = data.label.strip() or cat.label
    db.commit()
    return CategoryRead(key=cat.key, label=cat.label, builtin=False)


@categories_router.delete("/{key}")
def delete_category(key: str, db: Session = Depends(get_db)):
    if key in _BUILTIN_KEYS:
        raise HTTPException(400, "内置类别不可删除")
    cat = db.get(AssetCategory, key)
    if not cat:
        raise HTTPException(404, "类别不存在")
    if db.query(Asset).filter(Asset.kind == key).count() > 0:
        raise HTTPException(400, "该类别下还有素材，无法删除（请先改类别或删除这些素材）")
    db.delete(cat)
    db.commit()
    return {"ok": True}
