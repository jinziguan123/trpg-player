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
from app.services.vector_search import cosine_top_k

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


# ---- 结构感知切块 v2 -------------------------------------------------------
# PDF 逐行抽取留下两类噪声：①行内硬换行把词劈开（「临\n时生命值」），稀释嵌入又
# 浪费 token；②固定窗口无视小节边界，把「医学」的尾巴和「濒死」的定义切进同一块。
# 对策：先按「小节标题行」把每页分节（标题 = 短行且不含句读，如「自动武器射击」），
# 节内合并硬换行成连续文本，再对每节滑窗。识别不出标题的页整页当一节（退化为旧行为）。

_HEADING_MAX_CHARS = 14
# 含任一句读/终止标点的行不可能是标题（正文行几乎必含；漏判的标题只是并入上一节，无害）
_NON_HEADING_CHARS = "。，、；：？！…,;:!?"


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > _HEADING_MAX_CHARS:
        return False
    if any(c in line for c in _NON_HEADING_CHARS):
        return False
    return not line.replace(" ", "").isdigit()   # 纯页码行不是标题


def _join_lines(lines: list[str]) -> str:
    """合并 PDF 硬换行：中文直接相连（无空格分词，无损）；英文单词间补空格。"""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and ln[0].isascii() and ln[0].isalnum():
            out += " "
        out += ln
    return out


def _page_sections(text: str) -> list[str]:
    """把一页文本按小节标题分节，返回各节的连续文本。

    标题以【标题】前缀保留在节文里，随嵌入与注入一起出现——检索时标题词直接参与
    匹配，KP 也一眼可知条文出自哪一节。短于 MIN_CHUNK_CHARS 的节（页眉、孤立页码等
    版面噪声）由调用方过滤丢弃。
    """
    lines = text.split("\n")
    sections: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if _is_heading(ln) and cur:
            sections.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append(cur)

    out: list[str] = []
    for sec in sections:
        if _is_heading(sec[0]) and len(sec) > 1:
            out.append(f"【{sec[0]}】" + _join_lines(sec[1:]))
        else:
            out.append(_join_lines(sec))
    return [s for s in out if s]


def chunk_pages(
    pages: list[tuple[int, str]],
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """把逐页文本切成带页码的滑窗块（结构感知：先分节去硬换行，再节内滑窗）。

    按页切，保证页码引用精确；小节短于窗口即整节一块（语义完整），长节仍滑窗。
    返回 ``[{"page", "ordinal", "text"}, ...]``，ordinal 为全书顺序号。
    """
    chunks: list[dict] = []
    step = max(size - overlap, 1)
    ordinal = 0
    for page_no, raw in pages:
        for section in _page_sections(_clean(raw)):
            n = len(section)
            if n < MIN_CHUNK_CHARS:
                continue
            i = 0
            while i < n:
                piece = section[i : i + size].strip()
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


def _find_overlap(a: str, b: str, max_overlap: int = CHUNK_OVERLAP, min_overlap: int = 30) -> int:
    """返回 a 尾部与 b 头部的真实重叠长度（滑窗孪生块理论重叠恰为 CHUNK_OVERLAP，
    strip 可能略缩短）；不足 ``min_overlap`` 视为无重叠（不同小节的块不该被拼）。"""
    limit = min(len(a), len(b), max_overlap)
    for n in range(limit, min_overlap - 1, -1):
        if a[-n:] == b[:n]:
            return n
    return 0


def _merge_adjacent_hits(hits: list[dict]) -> list[dict]:
    """把**同页且 ordinal 相邻、文本确有滑窗重叠**的命中块合并成一段，score 取最大。

    否则同一长节的两个孪生块（含 80 字重叠）会占掉两个 top-k 名额、注入内容近半
    重复。三个条件缺一不可：ordinal 相邻但分属不同小节/不同页的块（内容不连续）
    绝不能拼在一起冒充连续原文。
    """
    if len(hits) <= 1:
        return hits
    hits = sorted(hits, key=lambda h: (h["rulebook_id"], h["ordinal"]))
    out = [dict(hits[0])]
    for h in hits[1:]:
        prev = out[-1]
        overlap = 0
        if (
            h["rulebook_id"] == prev["rulebook_id"]
            and h["page"] == prev["page"]
            and h["ordinal"] == prev["ordinal"] + 1
        ):
            overlap = _find_overlap(prev["text"], h["text"])
        if overlap:
            prev["text"] = prev["text"] + h["text"][overlap:]
            prev["score"] = max(prev["score"], h["score"])
            prev["ordinal"] = h["ordinal"]   # 三连及以上的相邻块继续并入
        else:
            out.append(dict(h))
    return out


def retrieve(
    db: Session,
    query: str,
    rule_system: str,
    k: int = 3,
    embedder: Embedder | None = None,
) -> list[dict]:
    """检索与 query 最相关的规则书片段（numpy 暴力余弦 top-k + 相邻块合并）。

    多取 2 个候选，把 ordinal 相邻的命中合并成完整段落后按分取前 ``k``。
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
    top = cosine_top_k(
        [r.embedding for r in rows], embedder.embed_query(query), k + 2,
    )
    hits = [
        {
            "text": rows[i].text,
            "page": rows[i].page,
            "score": score,
            "rulebook_id": rows[i].rulebook_id,
            "ordinal": rows[i].ordinal,
        }
        for i, score in top
    ]
    merged = _merge_adjacent_hits(hits)
    merged.sort(key=lambda h: h["score"], reverse=True)
    for h in merged:
        h.pop("ordinal", None)
    return merged[:k]


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
