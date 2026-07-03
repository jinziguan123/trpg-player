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
    # 用 float64 计算避免溢出。查询向量从严：任何非有限值都意味着嵌入器出了问题（正常嵌入
    # 不含 nan/inf），此时宁可不检索也不基于残缺向量给结果；零范数同理。
    qv = np.asarray(query_vec, dtype=np.float64)
    q_norm = np.linalg.norm(qv)
    if not np.isfinite(qv).all() or not np.isfinite(q_norm) or q_norm == 0:
        return []
    qn = qv / q_norm

    mat = np.frombuffer(b"".join(embeddings), dtype=np.float32)
    mat = mat.reshape(len(embeddings), -1).astype(np.float64)
    # 清理损坏 BLOB / 空文本产生的 nan·inf（否则 matmul 报 divide-by-zero/overflow/invalid，
    # 且 nan 进 argsort 排序不可预测）；这类坏行归零 → 相似度 0、自然排到末尾不被选中。
    mat = np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0)
    if mat.shape[1] != qn.shape[0]:
        return []  # 维度不一致（脏数据/换过嵌入模型）：宁可不检索也不给错误结果
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    safe = np.where(norms == 0, 1.0, norms)  # 零范数行除以 1 → 结果全 0（不触发除零告警）
    sims = (mat / safe) @ qn

    if weights is not None:
        w = np.nan_to_num(np.asarray(weights, dtype=np.float64), nan=1.0)
        sims = sims * w

    top = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in top]
