from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class LLMProvider(ABC):
    """LLM 服务提供者抽象接口"""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str: ...

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]: ...

    # ── 多模态（视觉）：默认不支持，视觉 Provider 覆盖 ──
    def supports_vision(self) -> bool:
        return False

    async def complete_vision(
        self, prompt: str, image_b64: str, mime: str, max_tokens: int | None = None,
    ) -> str:
        """据一张图片 + 文本提示生成文本（多模态）。非视觉 Provider 不实现。"""
        raise NotImplementedError("当前模型不支持多模态")
