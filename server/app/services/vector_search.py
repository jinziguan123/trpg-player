"""向量检索共用件：float32 BLOB 上的余弦 top-k。

规则书 RAG 与模组原文 RAG 共用这一段数学，避免两处各维护一份 numpy 检索代码。
两者规模都极小（千级块），暴力余弦即可，不引入向量库（与规则书 RAG 的选型一致）。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def cosine_top_k(
    embeddings: Sequence[bytes],
    query_vec: Sequence[float],
    k: int,
    weights: Sequence[float] | None = None,
) -> list[tuple[int, float]]:
    """对一组 float32 BLOB 向量做余弦 top-k，返回 ``[(行下标, 得分)]`` 按得分降序。

    ``weights`` 给定时逐行给相似度乘权后再排序（如「scene_hint 命中当前场景 ×1.3」
    的场景加权），返回的得分即加权后的值——调用方按下标回查原始行。
    """
    if not embeddings or k <= 0:
        return []
    qv = np.asarray(query_vec, dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    mat = np.frombuffer(b"".join(embeddings), dtype=np.float32)
    mat = mat.reshape(len(embeddings), -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    sims = (mat / (norms + 1e-8)) @ qn

    if weights is not None:
        sims = sims * np.asarray(weights, dtype=np.float32)

    top = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in top]
