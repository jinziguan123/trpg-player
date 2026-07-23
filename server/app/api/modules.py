import asyncio
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse, ModuleWrite
from app.services import hex_map, module_image_service, module_rag_service, module_service

logger = logging.getLogger(__name__)

# 上传解析任务表（内存态；本地单进程部署够用）：job_id → 进度/结果。
# 完成/失败的任务保留在表里供前端末轮轮询取结果，超量时按插入序淘汰最旧。
_upload_jobs: dict[str, dict] = {}
_MAX_UPLOAD_JOBS = 20


def _job_new() -> str:
    job_id = uuid.uuid4().hex
    while len(_upload_jobs) >= _MAX_UPLOAD_JOBS:
        _upload_jobs.pop(next(iter(_upload_jobs)), None)
    _upload_jobs[job_id] = {
        "status": "running", "stage": "排队中", "percent": 0,
        "detail": "", "result": None,
    }
    return job_id


def _job_update(job_id: str, **fields) -> None:
    job = _upload_jobs.get(job_id)
    if job is not None:
        job.update({k: v for k, v in fields.items() if v is not None})


def _rag_index_bg(module_id: str) -> None:
    """后台建模组原文索引：自开会话（请求会话已随响应关闭），失败状态由 service 落库。

    与规则书入库同一模式：同步函数交给 BackgroundTasks（在线程池执行），
    嵌入是 CPU 密集操作、不占事件循环。
    """
    from app.models.module import Module

    db = SessionLocal()
    try:
        module = db.get(Module, module_id)
        if module:
            module_rag_service.ingest_module(db, module)
    finally:
        db.close()


def _kick_rag_index(background, db: Session, module) -> None:
    """把模组置为 indexing 并安排后台建索引；无原文则跳过（fail-open，不阻塞上传/重建响应）。"""
    if not (module.raw_content or "").strip():
        return
    module.rag_status = "indexing"
    db.commit()
    background.add_task(_rag_index_bg, module.id)


router = APIRouter(prefix="/api/modules", tags=["modules"])


class ModuleImageRegenerateRequest(BaseModel):
    kind: str
    item_id: str
    field: str | None = None
    visual_state_key: str | None = None


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


async def _run_upload_job(
    job_id: str, raw_text: str, images: list[tuple[bytes, str]], rule_system: str,
) -> None:
    """后台执行模组解析全流程（首轮解析 → 查漏自检 → 入库 → 触发原文索引），逐段汇报进度。

    异常不外抛：一律落成 job 的 failed 状态 + 可读 detail（沿用旧同步端点的错误文案）。
    """
    try:
        _job_update(job_id, stage="AI 解析模组结构（大模组需数分钟）", percent=15)
        if images:
            # 图文/扫描件模组：用视觉模型据图片（+ 任何文字）识别提取
            parsed = await module_service.parse_module_images(images, rule_system, raw_text)
        else:
            parsed = await module_service.parse_module_text(
                raw_text, rule_system,
                on_progress=lambda note: _job_update(job_id, stage=note, percent=45),
            )
        _job_update(job_id, stage="查漏自检（对照原文补遗漏）", percent=60)
        parsed = await module_service.supplement_parse(raw_text, parsed, rule_system)
        parsed["rule_system"] = rule_system

        _job_update(job_id, stage="入库", percent=90)
        db = SessionLocal()
        try:
            module = module_service.create_module(db, parsed, raw_content=raw_text)
            # 解析成功后在后台自动建原文 RAG 索引（线程池执行，失败只落 rag_status）
            if (module.raw_content or "").strip():
                module.rag_status = "indexing"
                db.commit()
                asyncio.get_running_loop().run_in_executor(None, _rag_index_bg, module.id)
            result = ModuleUploadResponse(
                id=module.id, title=module.title, rule_system=module.rule_system,
                description=module.description, scenes_count=len(module.scenes),
                npcs_count=len(module.npcs), clues_count=len(module.clues),
            ).model_dump()
        finally:
            db.close()
        _job_update(job_id, status="done", stage="完成", percent=100, result=result)
    except json.JSONDecodeError:
        _job_update(job_id, status="failed",
                    detail="模型解析返回不完整（可能被截断），请重试；若反复失败可换更稳定的模型")
    except httpx.HTTPError as e:
        logger.warning("模组解析与模型连接失败：%s", e)
        _job_update(job_id, status="failed", detail="与模型的连接中断，模组解析未完成，请重试")
    except (ValueError, RuntimeError) as e:
        _job_update(job_id, status="failed", detail=f"模型解析失败：{e}")
    except Exception:
        logger.exception("模组解析任务失败: job=%s", job_id)
        _job_update(job_id, status="failed", detail="解析失败，请重试")


@router.post("/upload")
async def upload_module(
    files: list[UploadFile],
    rule_system: str = "coc",
):
    """上传模组：同步做文件抽取与格式校验（错误立即可见），解析转后台任务。

    返回 {job_id}；前端轮询 GET /upload/status/{job_id} 展示进度并取最终结果。
    """
    if not files:
        raise HTTPException(400, "请至少上传一个文件")

    _IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    # 模型是否支持看图——支持时才从 PDF 抽内嵌图（插图/手稿）一并喂给视觉解析
    try:
        from app.ai.llm_factory import get_llm
        vision = get_llm().supports_vision()
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

    job_id = _job_new()
    asyncio.create_task(_run_upload_job(job_id, raw_text, images, rule_system))
    return {"job_id": job_id}


@router.get("/upload/status/{job_id}")
def upload_status(job_id: str):
    """轮询上传解析任务：{status: running|done|failed, stage, percent, detail, result}。"""
    job = _upload_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或已过期")
    return job


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


class SceneMapPatch(BaseModel):
    scene_id: str
    q: int
    r: int
    biome: str | None = None


@router.patch("/{module_id}/scene-map")
def patch_scene_map(module_id: str, data: SceneMapPatch, db: Session = Depends(get_db)):
    """沙盘编辑：把场景移到指定 hex 格（KP 拖拽修正），可顺带改地貌。撞格等非法情形 400。"""
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    try:
        new_map = hex_map.set_scene_map(db, module, data.scene_id, data.q, data.r, data.biome)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"scene_id": data.scene_id, "map": new_map}


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


@router.post("/{module_id}/images/regenerate")
async def regenerate_module_image(
    module_id: str,
    data: ModuleImageRegenerateRequest,
    db: Session = Depends(get_db),
):
    """图片文件缺失时重新生成，并回写 scenes/npcs/clues 中的图片 URL。"""
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    try:
        url = await module_image_service.regenerate_module_image(
            db, module, data.kind, data.item_id, data.field,
            data.visual_state_key,
        )
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not url:
        raise HTTPException(503, "图片重新生成失败，请检查文生图配置后重试")
    return {"url": url, "kind": data.kind, "item_id": data.item_id}


@router.post("/{module_id}/rag/rebuild", response_model=ModuleRead)
def rebuild_module_rag(
    module_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """（重）建模组原文 RAG 索引：存量模组补建 / 失败后重试。

    立即置 rag_status=indexing 并返回，嵌入在后台进行；前端轮询模组的
    rag_status 观察 indexing → ready/failed。
    """
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    if not (module.raw_content or "").strip():
        raise HTTPException(400, "该模组没有留存原文（raw_content 为空），无法建原文索引")
    _kick_rag_index(background, db, module)
    return module




@router.delete("/{module_id}")
def delete_module(module_id: str, db: Session = Depends(get_db)):
    if not module_service.delete_module(db, module_id):
        raise HTTPException(404, "模组不存在")
    return {"ok": True}
