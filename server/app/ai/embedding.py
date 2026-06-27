"""文本嵌入抽象层。

与具体嵌入实现解耦（贴合平台「Provider 抽象」决策）：上层（规则书入库/检索）
只依赖 ``Embedder`` 接口，将来要换多语言模型或 API 嵌入都只替换一个实现。

默认实现 ``FastEmbedEmbedder`` 走纯 ONNX（fastembed，无 torch），中文模型
``BAAI/bge-small-zh-v1.5``（512 维，~120MB），全本地离线（首次使用下载一次模型）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBED_DIM = 512  # bge-small-zh-v1.5 输出 512 维（注意：英文 small 才是 384）


class Embedder(ABC):
    """文本嵌入提供者抽象接口。"""

    #: 向量维度（持久化/校验用）
    dim: int
    #: 模型标识（写进 Rulebook.embed_model，换模型时可识别旧索引失配）
    model_name: str

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """嵌入入库文档片段（passage 语义）。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """嵌入检索查询（query 语义；bge 类模型查询侧会自动加检索指令前缀）。"""


class FastEmbedEmbedder(Embedder):
    """fastembed(ONNX) 实现，默认中文 bge-small-zh-v1.5。"""

    def __init__(
        self, model_name: str = DEFAULT_EMBED_MODEL, dim: int = DEFAULT_EMBED_DIM
    ):
        self.model_name = model_name
        self.dim = dim
        self._model = None  # 懒加载：首次嵌入时才构造（避免 import/启动即下模型）

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding  # 懒导入，未装也不影响其余功能

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure()
        return [v.tolist() for v in model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure()
        return next(iter(model.query_embed([text]))).tolist()


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """进程级单例：复用已加载的嵌入模型，避免重复构造/下载。"""
    global _embedder
    if _embedder is None:
        _embedder = FastEmbedEmbedder()
    return _embedder
