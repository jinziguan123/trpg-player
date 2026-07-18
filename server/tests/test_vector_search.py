"""cosine_top_k 的数值鲁棒性测试：坏数据（零范数/nan/inf）不得告警、不得产出 nan、不得崩。"""

import warnings

import numpy as np

from app.services.vector_search import cosine_top_k


def _blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def test_正常检索按相似度降序():
    q = [1.0, 0.0]
    embs = [_blob([1.0, 0.0]), _blob([0.0, 1.0]), _blob([0.9, 0.1])]
    top = cosine_top_k(embs, q, k=2)
    assert [i for i, _ in top] == [0, 2]  # 与 q 最接近的两行


def test_零范数行不告警且排末尾():
    q = [1.0, 0.0]
    embs = [_blob([0.0, 0.0]), _blob([1.0, 0.0])]  # 第 0 行零向量
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 任何 RuntimeWarning 都会让测试失败
        top = cosine_top_k(embs, q, k=2)
    assert top[0][0] == 1  # 有效行在前
    assert all(np.isfinite(score) for _, score in top)  # 无 nan


def test_nan与inf行被清理不崩():
    q = [1.0, 0.0, 0.0]
    embs = [
        _blob([float("nan"), 1.0, 0.0]),
        _blob([float("inf"), 0.0, 0.0]),
        _blob([1.0, 0.0, 0.0]),  # 唯一干净且贴合 q 的行
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        top = cosine_top_k(embs, q, k=3)
    assert top[0][0] == 2
    assert all(np.isfinite(score) for _, score in top)


def test_超大有限坏行不告警不被选():
    """损坏 BLOB 可能被重解读成超大**有限** float32（非 nan/inf，逃过 nan_to_num）——
    也不得告警、不得产出 nan、不得盖过正常行。"""
    q = [1.0, 0.0]
    huge = 3.0e38  # 接近 float32 上限的有限值
    embs = [_blob([huge, huge]), _blob([1.0, 0.0])]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        top = cosine_top_k(embs, q, k=2)
    assert top[0][0] == 1  # 正常行在前
    assert all(np.isfinite(score) for _, score in top)


def test_查询零向量返回空():
    assert cosine_top_k([_blob([1.0, 0.0])], [0.0, 0.0], k=1) == []


def test_查询含nan返回空():
    assert cosine_top_k([_blob([1.0, 0.0])], [float("nan"), 1.0], k=1) == []


def test_维度不一致返回空():
    # 查询 3 维、库向量 2 维 → 不检索而非报错
    assert cosine_top_k([_blob([1.0, 0.0])], [1.0, 0.0, 0.0], k=1) == []


def test_空输入():
    assert cosine_top_k([], [1.0], k=3) == []
    assert cosine_top_k([_blob([1.0])], [1.0], k=0) == []


def test_场景加权提升命中行():
    q = [1.0, 0.0]
    embs = [_blob([1.0, 0.0]), _blob([0.95, 0.05])]
    # 第 1 行加权 ×2 后应反超第 0 行
    top = cosine_top_k(embs, q, k=2, weights=[1.0, 2.0])
    assert top[0][0] == 1
