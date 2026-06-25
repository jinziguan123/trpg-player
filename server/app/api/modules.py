from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.module import ModuleRead, ModuleUploadResponse
from app.services import module_service

router = APIRouter(prefix="/api/modules", tags=["modules"])


@router.post("/upload", response_model=ModuleUploadResponse)
async def upload_module(
    file: UploadFile,
    rule_system: str = "coc",
    db: Session = Depends(get_db),
):
    content = await file.read()
    raw_text = content.decode("utf-8")

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
