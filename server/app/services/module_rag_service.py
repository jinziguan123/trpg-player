"""模组原文 RAG：raw_content 切块（带场景归属）→ 嵌入入库 → 场景加权检索。

镜像规则书 RAG（rulebook_service）的形态：同一套 Embedder 抽象、同一段余弦
top-k（vector_search.cosine_top_k）、同一个 indexing/ready/failed 状态机
（状态记在 modules.rag_status，空字符串=未建索引的存量模组）。差异只有两点：
1. 模组原文没有页码，按全文滑窗切块，切块后用场景标题在块内模糊匹配回填
   scene_hint（该块的章节归属场景）；
2. 检索时 scene_hint == 当前场景的块得分 ×SCENE_BOOST，优先取当前场景的原文。
"""

from __future__ import annotations

import logging
import re

import numpy as np
from sqlalchemy.orm import Session

from app.ai.embedding import Embedder, get_embedder
from app.models.module import Module, ModuleChunk
from app.services.vector_search import cosine_top_k

logger = logging.getLogger(__name__)

# 切块参数：~500 字一块、10% 重叠（与规则书取同一量级，保证语义完整与检索粒度）。
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
MIN_CHUNK_CHARS = 30
# 当前场景块的加权系数：让被动注入的摘录大概率是当前场景文本（泄密三重防线之一）。
SCENE_BOOST = 1.3

# 场景标题模糊匹配用的归一化：抹掉空白与常见标点/括注，只比正文字符。
_NORM_RE = re.compile(r"[\s　，。、：:；;！!？?—\-·・~～*#>《》〈〉()（）\[\]【】「」『』\"'“”‘’]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub("", text or "")


def chunk_text(
    text: str,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """把模组原文切成滑窗块，返回 ``[{"ordinal", "text"}, ...]``。

    与规则书按页切不同，模组原文无页码，直接对全文滑窗；压掉连续空行后
    过短的碎块（目录残渣、分隔线等）丢弃。
    """
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    chunks: list[dict] = []
    step = max(size - overlap, 1)
    i, n, ordinal = 0, len(text), 0
    while i < n:
        piece = text[i : i + size].strip()
        if len(piece) >= MIN_CHUNK_CHARS:
            chunks.append({"ordinal": ordinal, "text": piece})
            ordinal += 1
        if i + size >= n:
            break
        i += step
    return chunks


def backfill_scene_hints(chunks: list[dict], scenes: list[dict] | None) -> list[dict]:
    """用场景标题在块内模糊匹配，为每块回填 scene_hint（纯函数，返回新列表）。

    规则：按块序扫描——块内命中某场景标题（归一化后子串匹配）即视为进入该章节，
    命中多个时取「在块内出现位置最靠后」的那个（后出现的标题统辖其后的正文）；
    未命中的块沿用上一块的归属（章节正文跨多块）。单字标题不参与匹配（误报太高）。
    """
    titles: list[tuple[str, str]] = []  # (归一化标题, scene_id)
    for s in scenes or []:
        sid = s.get("id")
        title = _norm(str(s.get("title") or s.get("name") or ""))
        if sid and len(title) >= 2:
            titles.append((title, sid))

    result: list[dict] = []
    current: str | None = None
    for c in chunks:
        body = _norm(c.get("text") or "")
        best_pos, best_id = -1, None
        for title, sid in titles:
            pos = body.rfind(title)
            if pos > best_pos:
                best_pos, best_id = pos, sid
        if best_id is not None:
            current = best_id
        result.append({**c, "scene_hint": current})
    return result


def ingest_module(
    db: Session,
    module: Module,
    embedder: Embedder | None = None,
) -> Module:
    """对模组原文建（或重建）RAG 索引：切块→回填场景→嵌入→写 ModuleChunk。

    成功置 rag_status=ready，失败置 failed。fail-open：本函数吞掉一切异常只落
    状态，绝不向上抛——建索引失败不得阻塞模组解析/跑团主流程。
    """
    embedder = embedder or get_embedder()
    try:
        chunks = backfill_scene_hints(
            chunk_text(module.raw_content or ""), module.scenes,
        )
        if not chunks:
            raise ValueError("模组没有可切块的原文（raw_content 为空或过短）")

        vectors = embedder.embed_passages([c["text"] for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("嵌入数量与切块数量不一致")

        # 重建：先清旧块再写新块（同一事务，失败回滚不留半截索引）
        db.query(ModuleChunk).filter(ModuleChunk.module_id == module.id).delete()
        for c, vec in zip(chunks, vectors):
            arr = np.asarray(vec, dtype=np.float32)
            db.add(
                ModuleChunk(
                    module_id=module.id,
                    scene_hint=c.get("scene_hint"),
                    ordinal=c["ordinal"],
                    text=c["text"],
                    embedding=arr.tobytes(),
                )
            )
        module.rag_status = "ready"
        db.commit()
        db.refresh(module)
        logger.info("模组原文索引完成：%s（%d 块）", module.title, len(chunks))
    except Exception:  # noqa: BLE001 — fail-open：失败只落状态，不阻塞主流程
        db.rollback()
        module.rag_status = "failed"
        db.commit()
        logger.exception("模组原文索引失败：%s", module.title)
    return module


def retrieve(
    db: Session,
    module_id: str,
    query: str,
    k: int = 3,
    scene_id: str | None = None,
    embedder: Embedder | None = None,
) -> list[dict]:
    """检索与 query 最相关的模组原文片段（当前场景块 ×SCENE_BOOST 加权）。

    返回 ``[{"text", "scene_hint", "score", "ordinal"}, ...]`` 按加权得分降序；
    未建索引/无命中返回空列表（上层据此优雅降级）。
    """
    query = (query or "").strip()
    if not query:
        return []

    rows = (
        db.query(ModuleChunk)
        .filter(ModuleChunk.module_id == module_id)
        .all()
    )
    if not rows:
        return []

    embedder = embedder or get_embedder()
    weights = None
    if scene_id:
        weights = [
            SCENE_BOOST if r.scene_hint == scene_id else 1.0 for r in rows
        ]
    top = cosine_top_k(
        [r.embedding for r in rows], embedder.embed_query(query), k, weights=weights,
    )
    return [
        {
            "text": rows[i].text,
            "scene_hint": rows[i].scene_hint,
            "score": score,
            "ordinal": rows[i].ordinal,
        }
        for i, score in top
    ]
