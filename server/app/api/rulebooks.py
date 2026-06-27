import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.rulebook import Rulebook
from app.schemas.rulebook import RuleSearchResponse, RulebookRead
from app.services import rulebook_service

router = APIRouter(prefix="/api/rulebooks", tags=["rulebooks"])
logger = logging.getLogger(__name__)


def _ingest_bg(rulebook_id: str, pdf_bytes: bytes) -> None:
    """后台入库：自开会话（请求会话已随响应关闭），失败状态由 service 落库。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        book = db.get(Rulebook, rulebook_id)
        if book:
            rulebook_service.ingest_rulebook(db, book, pdf_bytes)
    finally:
        db.close()


@router.post("/upload", response_model=RulebookRead)
async def upload_rulebook(
    background: BackgroundTasks,
    file: UploadFile,
    title: str | None = None,
    rule_system: str = "coc",
    db: Session = Depends(get_db),
):
    """上传规则书 PDF：立即建 indexing 记录并返回，嵌入索引在后台进行。

    前端可轮询 GET /api/rulebooks 观察 status 由 indexing → ready/failed。
    """
    content = await file.read()
    if not content:
        raise HTTPException(400, "空文件")
    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        raise HTTPException(422, "目前只支持 PDF 规则书")

    book = Rulebook(
        title=title or file.filename or "规则书",
        rule_system=rule_system,
        status="indexing",
    )
    db.add(book)
    db.commit()
    db.refresh(book)

    background.add_task(_ingest_bg, book.id, content)
    return book


@router.get("", response_model=list[RulebookRead])
def list_rulebooks(db: Session = Depends(get_db)):
    return rulebook_service.list_rulebooks(db)


@router.get("/search", response_model=RuleSearchResponse)
def search_rules(
    q: str,
    rule_system: str = "coc",
    k: int = 3,
    db: Session = Depends(get_db),
):
    """调试用：直接对规则书做一次检索，观察命中片段与分数。"""
    hits = rulebook_service.retrieve(db, q, rule_system, k=k)
    return RuleSearchResponse(query=q, hits=hits)


@router.delete("/{rulebook_id}")
def delete_rulebook(rulebook_id: str, db: Session = Depends(get_db)):
    if not rulebook_service.delete_rulebook(db, rulebook_id):
        raise HTTPException(404, "规则书不存在")
    return {"ok": True}
