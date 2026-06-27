"""规则书 RAG：PDF 入库（切块 + 嵌入）与按需检索。

设计取舍（见技术选型）：规模极小（400 页 ≈ 1–2k chunk），不引入独立向量库，
向量以 float32 BLOB 存进现有 SQLite，查询时 numpy 暴力余弦 top-k，零新基础设施。
嵌入走可插拔 ``Embedder``（默认 fastembed + 中文 bge-small-zh）。
"""

from __future__ import annotations

import io
import logging
import re

import numpy as np
from sqlalchemy.orm import Session

from app.ai.embedding import Embedder, get_embedder
from app.models.rulebook import RuleChunk, Rulebook

logger = logging.getLogger(__name__)

# 切块参数：中文按字符近似，~500 字一块、~80 字重叠，兼顾语义完整与检索粒度。
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
MIN_CHUNK_CHARS = 30


def extract_pages(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """用 pypdf 抽取每页文本，返回 ``[(page_no, text), ...]``（page_no 从 1 起）。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # 个别坏页不应中断整本入库
            logger.warning("规则书第 %d 页文本抽取失败，跳过", i)
            text = ""
        pages.append((i, text))
    return pages


def _clean(text: str) -> str:
    # 合并多余空白；保留换行用于句界，但压掉连续空行
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def chunk_pages(
    pages: list[tuple[int, str]],
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """把逐页文本切成带页码的滑窗块。

    按页切，保证页码引用精确；页内用 ``size``/``overlap`` 滑窗。返回
    ``[{"page", "ordinal", "text"}, ...]``，ordinal 为全书顺序号。
    """
    chunks: list[dict] = []
    step = max(size - overlap, 1)
    ordinal = 0
    for page_no, raw in pages:
        text = _clean(raw)
        if len(text) < MIN_CHUNK_CHARS:
            continue
        i = 0
        n = len(text)
        while i < n:
            piece = text[i : i + size].strip()
            if len(piece) >= MIN_CHUNK_CHARS:
                chunks.append({"page": page_no, "ordinal": ordinal, "text": piece})
                ordinal += 1
            if i + size >= n:
                break
            i += step
    return chunks


def ingest_rulebook(
    db: Session,
    rulebook: Rulebook,
    pdf_bytes: bytes,
    embedder: Embedder | None = None,
) -> Rulebook:
    """对已建好的 Rulebook 记录做入库：抽取→切块→嵌入→写 RuleChunk。

    成功置 status=ready，失败置 failed 并记 error。调用方负责先创建 Rulebook
    （status=indexing）并提交，便于前端轮询进度。
    """
    embedder = embedder or get_embedder()
    try:
        pages = extract_pages(pdf_bytes)
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("未从 PDF 抽取到可用文本（可能是扫描件，需 OCR）")

        vectors = embedder.embed_passages([c["text"] for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("嵌入数量与切块数量不一致")

        for c, vec in zip(chunks, vectors):
            arr = np.asarray(vec, dtype=np.float32)
            db.add(
                RuleChunk(
                    rulebook_id=rulebook.id,
                    rule_system=rulebook.rule_system,
                    page=c["page"],
                    ordinal=c["ordinal"],
                    text=c["text"],
                    embedding=arr.tobytes(),
                )
            )

        rulebook.page_count = len(pages)
        rulebook.chunk_count = len(chunks)
        rulebook.embed_model = embedder.model_name
        rulebook.status = "ready"
        rulebook.error = ""
        db.commit()
        db.refresh(rulebook)
        logger.info(
            "规则书入库完成：%s（%d 页 / %d 块）",
            rulebook.title, rulebook.page_count, rulebook.chunk_count,
        )
    except Exception as exc:  # noqa: BLE001 — 入库失败要落状态而非吞掉
        db.rollback()
        rulebook.status = "failed"
        rulebook.error = str(exc)[:500]
        db.commit()
        logger.exception("规则书入库失败：%s", rulebook.title)
    return rulebook


def retrieve(
    db: Session,
    query: str,
    rule_system: str,
    k: int = 3,
    embedder: Embedder | None = None,
) -> list[dict]:
    """检索与 query 最相关的规则书片段（numpy 暴力余弦 top-k）。

    返回 ``[{"text", "page", "score", "rulebook_id"}, ...]``，按相关度降序。
    无已就绪的规则书时返回空列表（上层据此优雅降级）。
    """
    query = (query or "").strip()
    if not query:
        return []

    rows = (
        db.query(RuleChunk)
        .filter(RuleChunk.rule_system == rule_system)
        .all()
    )
    if not rows:
        return []

    embedder = embedder or get_embedder()
    qv = np.asarray(embedder.embed_query(query), dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    mat = np.frombuffer(b"".join(r.embedding for r in rows), dtype=np.float32)
    mat = mat.reshape(len(rows), -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mn = mat / (norms + 1e-8)
    sims = mn @ qn

    top = np.argsort(-sims)[: max(k, 0)]
    return [
        {
            "text": rows[i].text,
            "page": rows[i].page,
            "score": float(sims[i]),
            "rulebook_id": rows[i].rulebook_id,
        }
        for i in top
    ]


def has_rulebook(db: Session, rule_system: str) -> bool:
    """该规则系统是否已有可检索（ready）的规则书。"""
    return (
        db.query(Rulebook.id)
        .filter(Rulebook.rule_system == rule_system, Rulebook.status == "ready")
        .first()
        is not None
    )


def list_rulebooks(db: Session) -> list[Rulebook]:
    return db.query(Rulebook).order_by(Rulebook.created_at.desc()).all()


def delete_rulebook(db: Session, rulebook_id: str) -> bool:
    book = db.get(Rulebook, rulebook_id)
    if not book:
        return False
    # 显式删切块（SQLite 默认不强制级联，且测试库未必开外键）
    db.query(RuleChunk).filter(RuleChunk.rulebook_id == rulebook_id).delete()
    db.delete(book)
    db.commit()
    return True
