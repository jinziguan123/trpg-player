from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse, ModuleWrite
from app.services import map_service, module_service


class VariantMapRequest(BaseModel):
    hint: str = ""

router = APIRouter(prefix="/api/modules", tags=["modules"])


def _decode_text(content: bytes) -> str:
    """容错解码文本：UTF-8(含BOM) → GB18030(覆盖 GBK/GB2312) → Big5 → UTF-16 依次尝试。

    中文 txt 常是 GBK 编码（非 UTF-8），直接 utf-8 解码会抛 UnicodeDecodeError。
    全部失败（多半是二进制文件）时抛友好错误而非 500。
    """
    for enc in ("utf-8-sig", "gb18030", "big5", "utf-16"):
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise HTTPException(422, "无法识别文件编码（可能是二进制文件）。请上传 PDF / Word(.docx) / 纯文本(UTF-8 或 GBK)")


def _read_pdf_text(content: bytes) -> str:
    """提取 PDF 文字层（扫描件可能为空）。"""
    import io

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _select_pdf_images(reader, max_images: int = 8, min_bytes: int = 3000) -> list[tuple[bytes, str]]:
    """从 PdfReader 抽取内嵌位图（地图/手稿插图），按体积降序取前 N 张。

    用 pypdf 自带的 page.images（无需引入额外依赖）；过滤掉过小的图标/装饰，
    地图通常是页面里最大的那张图，故按 data 体积排序。
    """
    imgs: list[tuple[bytes, str]] = []
    for page in reader.pages:
        for img in getattr(page, "images", None) or []:
            data = getattr(img, "data", b"") or b""
            if len(data) < min_bytes:
                continue
            name = (getattr(img, "name", "") or "").lower()
            if name.endswith(".png"):
                mime = "image/png"
            elif name.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif name.endswith(".gif"):
                mime = "image/gif"
            else:
                mime = "image/png"
            imgs.append((data, mime))
    imgs.sort(key=lambda t: len(t[0]), reverse=True)
    return imgs[:max_images]


def _extract_pdf_images(content: bytes, max_images: int = 8) -> list[tuple[bytes, str]]:
    import io

    from pypdf import PdfReader
    return _select_pdf_images(PdfReader(io.BytesIO(content)), max_images=max_images)


def _extract_doc_text(content: bytes, filename: str) -> str:
    """docx / 旧版 doc / 纯文本（含 GBK 等）解码。PDF 由调用方单独处理。"""
    fn = filename.lower()
    if fn.endswith(".docx"):
        import io

        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if fn.endswith(".doc"):
        raise HTTPException(422, f"「{filename}」是旧版 Word(.doc)，暂不支持；请另存为 .docx 或 PDF 后上传")
    try:
        return _decode_text(content)
    except HTTPException as e:
        raise HTTPException(e.status_code, f"「{filename}」{e.detail}")


@router.post("/upload", response_model=ModuleUploadResponse)
async def upload_module(
    files: list[UploadFile],
    rule_system: str = "coc",
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(400, "请至少上传一个文件")

    _IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    # 模型是否支持看图——支持时才从 PDF 抽内嵌图（地图/插图）一并喂给视觉解析
    try:
        vision = map_service.get_llm().supports_vision()
    except Exception:
        vision = False

    parts: list[str] = []
    images: list[tuple[bytes, str]] = []   # (bytes, mime) —— 图片走多模态视觉解析
    for f in files:
        fn = (f.filename or "").lower()
        ct = f.content_type or ""
        content = await f.read()
        if ct.startswith("image/") or fn.endswith(_IMG_EXT):
            images.append((content, ct or "image/png"))
        elif fn.endswith(".pdf") or ct == "application/pdf":
            text = _read_pdf_text(content)
            pdf_imgs = _extract_pdf_images(content) if vision else []
            if text.strip():
                parts.append(f"=== 文件：{f.filename} ===\n{text}" if len(files) > 1 else text)
            images.extend(pdf_imgs)
            if not text.strip() and not pdf_imgs:
                raise HTTPException(
                    422,
                    f"「{f.filename}」似乎是扫描件（无可提取文字）。请改用含文字层的 PDF，"
                    "或切换到支持视觉的多模态模型后重试（届时会直接识图）",
                )
        else:
            text = _extract_doc_text(content, f.filename or "")
            parts.append(f"=== 文件：{f.filename} ===\n{text}" if len(files) > 1 else text)

    raw_text = "\n\n".join(parts)

    try:
        if images:
            # 图文/扫描件模组：用视觉模型据图片（+ 任何文字）识别提取
            parsed = await module_service.parse_module_images(images, rule_system, raw_text)
        else:
            parsed = await module_service.parse_module_text(raw_text, rule_system)
    except ValueError as e:
        raise HTTPException(400, str(e))
    parsed["rule_system"] = rule_system

    module = module_service.create_module(db, parsed, raw_content=raw_text)

    return ModuleUploadResponse(
        id=module.id,
        title=module.title,
        rule_system=module.rule_system,
        description=module.description,
        scenes_count=len(module.scenes),
        npcs_count=len(module.npcs),
        clues_count=len(module.clues),
    )


@router.get("", response_model=list[ModuleRead])
def list_modules(db: Session = Depends(get_db)):
    return module_service.list_modules(db)


@router.post("", response_model=ModuleRead)
def create_module(data: ModuleWrite, db: Session = Depends(get_db)):
    """手动新建模组（结构化内容，非 PDF 解析）。"""
    if not data.title.strip():
        raise HTTPException(400, "模组标题不能为空")
    return module_service.create_module(db, data.model_dump())


@router.put("/{module_id}", response_model=ModuleRead)
def update_module(module_id: str, data: ModuleWrite, db: Session = Depends(get_db)):
    """整体编辑模组结构化内容。"""
    if not data.title.strip():
        raise HTTPException(400, "模组标题不能为空")
    module = module_service.update_module(db, module_id, data.model_dump())
    if not module:
        raise HTTPException(404, "模组不存在")
    return module


@router.get("/{module_id}", response_model=ModuleRead)
def get_module(module_id: str, db: Session = Depends(get_db)):
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    return module


@router.post("/{module_id}/maps", response_model=ModuleRead)
async def generate_maps(module_id: str, force: bool = False, db: Session = Depends(get_db)):
    """为模组各场景生成像素地图（已有的默认跳过，force=true 全部重生成）。逐场景调用 AI，可能较慢。"""
    module = await map_service.generate_maps_for_module(db, module_id, force=force)
    if not module:
        raise HTTPException(404, "模组不存在")
    return module


@router.post("/{module_id}/scenes/{scene_id}/variant-map")
async def variant_map(module_id: str, scene_id: str, body: VariantMapRequest, db: Session = Depends(get_db)):
    """据某场景的基础地图 + 一句变化说明，AI 生成变体地图（不落库，前端载入编辑器后再保存到对应 state）。"""
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
    if not scene:
        raise HTTPException(404, "场景不存在")
    base = scene.get("map")
    if not base:
        raise HTTPException(400, "该场景还没有基础地图，请先生成或手绘基础地图")
    return await map_service.generate_variant_map(base, body.hint)


@router.post("/{module_id}/scenes/{scene_id}/map-from-image")
async def map_from_image(module_id: str, scene_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """多模态：上传地图图片（或含地图图片的 PDF），由视觉 LLM 转成本项目的瓦片地图（不落库，前端载入编辑器后再保存）。"""
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
    if not scene:
        raise HTTPException(404, "场景不存在")
    ct = file.content_type or ""
    fn = (file.filename or "").lower()
    data = await file.read()
    if ct == "application/pdf" or fn.endswith(".pdf"):
        # PDF：抽出体积最大的内嵌图（通常就是地图）来转换
        imgs = _extract_pdf_images(data, max_images=1)
        if not imgs:
            raise HTTPException(400, "PDF 中未找到可用的地图图片")
        data, ct = imgs[0]
    elif not ct.startswith("image/"):
        raise HTTPException(400, "请上传图片，或含地图图片的 PDF")
    try:
        return await map_service.generate_map_from_image(data, ct, scene)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{module_id}")
def delete_module(module_id: str, db: Session = Depends(get_db)):
    if not module_service.delete_module(db, module_id):
        raise HTTPException(404, "模组不存在")
    return {"ok": True}
