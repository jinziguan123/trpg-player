import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse, ModuleWrite
from app.services import map_service, module_service

logger = logging.getLogger(__name__)

# 后台地图生成任务：持引用防 GC；in-flight 去重，避免同一模组并发全量生成。
_map_gen_tasks: set = set()
_map_gen_inflight: set[str] = set()


def _kick_background_map_gen(module_id: str) -> None:
    """上传解析成功后，非阻塞地在后台为模组生成全部场景地图（不阻塞上传响应）。"""
    if not module_id or module_id in _map_gen_inflight:
        return
    _map_gen_inflight.add(module_id)

    async def _run():
        db = SessionLocal()
        try:
            await map_service.generate_maps_for_module(db, module_id)
        except Exception:
            logger.exception("后台生成模组地图失败：module=%s", module_id)
        finally:
            db.close()
            _map_gen_inflight.discard(module_id)

    task = asyncio.create_task(_run())
    _map_gen_tasks.add(task)
    task.add_done_callback(_map_gen_tasks.discard)


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


def _normalize_image(pil, data: bytes) -> tuple[bytes, str] | None:
    """把 PDF 里抽出的图规整成「视觉接口一定能解码」的 RGB JPEG（限尺寸），失败返回 None。

    pypdf 的 img.data 对某些图（CMYK/索引色/特殊滤镜/掩膜）并非合法的独立 PNG/JPEG，
    直接当 image/png 发出去会被接口 400 拒收。统一经 Pillow 重新编码可规避，并顺带压体积。
    """
    import io

    from PIL import Image

    im = pil
    if im is None:
        try:
            im = Image.open(io.BytesIO(data))
        except Exception:
            return None
    try:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((1600, 1600))  # 限制最长边，避免过大 payload
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return None


def _select_pdf_images(reader, max_images: int = 8, min_bytes: int = 3000) -> list[tuple[bytes, str]]:
    """从 PdfReader 抽取内嵌位图（地图/手稿插图），按体积降序取前 N 张并规整为合法 JPEG。

    地图通常是页面里最大的那张图，故按原始体积排序优先；每张经 Pillow 重新编码
    （统一 RGB JPEG、限尺寸），无法解码的直接跳过——避免把畸形图发给视觉接口触发 400。
    """
    candidates: list[tuple[object, bytes, int]] = []
    for page in reader.pages:
        for img in getattr(page, "images", None) or []:
            data = getattr(img, "data", b"") or b""
            if len(data) < min_bytes:
                continue
            try:
                pil = img.image  # pypdf 已解码的 PIL Image（访问即解码，可能抛错）
            except Exception:
                pil = None
            candidates.append((pil, data, len(data)))
    candidates.sort(key=lambda t: t[2], reverse=True)
    out: list[tuple[bytes, str]] = []
    for pil, data, _ in candidates:
        norm = _normalize_image(pil, data)
        if norm:
            out.append(norm)
        if len(out) >= max_images:
            break
    return out


def _extract_pdf_images(content: bytes, max_images: int = 8) -> list[tuple[bytes, str]]:
    import io

    from pypdf import PdfReader
    return _select_pdf_images(PdfReader(io.BytesIO(content)), max_images=max_images)


def _convert_doc_to_text(content: bytes) -> str | None:
    """把旧版二进制 Word(.doc) 转成纯文本。

    .doc 是 OLE 复合格式，python-docx 读不了；这里复用系统现成转换器（不引入重依赖）：
    优先 macOS 自带 textutil，其次 LibreOffice(soffice/libreoffice)。都没有则返回 None。
    """
    import os
    import shutil
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.doc")
        with open(src, "wb") as f:
            f.write(content)

        # macOS：textutil 直接转纯文本到 stdout
        if shutil.which("textutil"):
            try:
                out = subprocess.run(
                    ["textutil", "-convert", "txt", "-stdout", src],
                    capture_output=True, timeout=60,
                )
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.decode("utf-8", errors="replace")
            except (subprocess.SubprocessError, OSError):
                pass

        # 跨平台：LibreOffice headless 转 txt 文件再读
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            try:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "txt:Text", "--outdir", tmp, src],
                    capture_output=True, timeout=120,
                )
                txt = os.path.join(tmp, "in.txt")
                if os.path.exists(txt):
                    with open(txt, "rb") as f:
                        data = f.read()
                    if data.strip():
                        return _decode_text(data)
            except (subprocess.SubprocessError, OSError, HTTPException):
                pass

    return None


def _extract_doc_text(content: bytes, filename: str) -> str:
    """docx / 旧版 doc / 纯文本（含 GBK 等）解码。PDF 由调用方单独处理。"""
    fn = filename.lower()
    if fn.endswith(".docx"):
        import io

        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if fn.endswith(".doc"):
        text = _convert_doc_to_text(content)
        if text and text.strip():
            return text
        raise HTTPException(
            422,
            f"「{filename}」是旧版 Word(.doc)，本机未找到可用的转换器（macOS 自带 textutil "
            "或安装 LibreOffice）。请另存为 .docx / PDF 后上传，或安装 LibreOffice 后重试",
        )
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
    except RuntimeError as e:
        # 视觉/文本接口报错（如图片被拒、超限）：回可读信息而非裸 500
        raise HTTPException(502, f"模型解析失败：{e}")
    parsed["rule_system"] = rule_system

    module = module_service.create_module(db, parsed, raw_content=raw_text)

    # 解析成功后在后台自动生成全部场景地图（非阻塞：上传立即返回，地图稍后陆续就绪）
    _kick_background_map_gen(module.id)

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


@router.get("/difficulties")
def list_difficulties():
    """模组难度枚举（单一真源），供编辑下拉与筛选用。"""
    return list(module_service.MODULE_DIFFICULTIES)


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
