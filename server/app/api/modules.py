from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse
from app.services import module_service

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


@router.get("/{module_id}", response_model=ModuleRead)
def get_module(module_id: str, db: Session = Depends(get_db)):
    module = module_service.get_module(db, module_id)
    if not module:
        raise HTTPException(404, "模组不存在")
    return module


@router.delete("/{module_id}")
def delete_module(module_id: str, db: Session = Depends(get_db)):
    if not module_service.delete_module(db, module_id):
        raise HTTPException(404, "模组不存在")
    return {"ok": True}
