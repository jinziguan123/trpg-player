from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse, ModuleWrite
from app.services import map_service, module_service


class VariantMapRequest(BaseModel):
    hint: str = ""

router = APIRouter(prefix="/api/modules", tags=["modules"])


async def _extract_text(file: UploadFile) -> str:
    content = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.strip()) < 50:
            raise HTTPException(
                422,
                f"「{file.filename}」似乎是扫描件（无可提取文字），请使用含有文字层的 PDF，或转换为 Word/文本格式后上传",
            )
        return text
    elif filename.endswith(".docx"):
        import io
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        return content.decode("utf-8")


@router.post("/upload", response_model=ModuleUploadResponse)
async def upload_module(
    files: list[UploadFile],
    rule_system: str = "coc",
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(400, "请至少上传一个文件")

    parts: list[str] = []
    for f in files:
        text = await _extract_text(f)
        if len(files) > 1:
            parts.append(f"=== 文件：{f.filename} ===\n{text}")
        else:
            parts.append(text)

    raw_text = "\n\n".join(parts)

    parsed = await module_service.parse_module_text(raw_text, rule_system)
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
    """多模态：上传模组自带的地图图片，由视觉 LLM 转成本项目的瓦片地图（不落库，前端载入编辑器后再保存）。"""
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
    if not scene:
        raise HTTPException(404, "场景不存在")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "请上传图片")
    data = await file.read()
    try:
        return await map_service.generate_map_from_image(data, file.content_type, scene)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{module_id}")
def delete_module(module_id: str, db: Session = Depends(get_db)):
    if not module_service.delete_module(db, module_id):
        raise HTTPException(404, "模组不存在")
    return {"ok": True}
