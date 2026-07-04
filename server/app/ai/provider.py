from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """一次完整的工具调用（流式聚合完成后才产出，arguments 已解析为 dict）。"""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class StreamDelta:
    """stream_chat 的流式增量：文本片段或一次完整的工具调用。

    - kind="text"：text 为本次增量文本；
    - kind="tool_call"：tool_call 为聚合完成的调用（供应商的参数分片由 Provider 内部聚合，
      调用方永远拿到完整调用，不需要自己拼 JSON 片段）。
    """

    kind: str  # "text" | "tool_call"
    text: str = ""
    tool_call: ToolCall | None = None


class LLMProvider(ABC):
    """LLM 服务提供者抽象接口"""

    # 最近一次调用的服务端真实 usage（{prompt_tokens, completion_tokens, total_tokens, ...}）；
    # 不支持的 Provider 保持 None，调用方回落启发式估算。
    last_usage: dict | None = None

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

    # ── 工具调用（function calling）：默认不支持，具备能力的 Provider 覆盖 ──
    # 消息与工具的**统一格式为 OpenAI 风格**（tools=function schema 列表；对话里 assistant
    # 消息可带 tool_calls、工具结果用 role="tool" + tool_call_id）——非 OpenAI 协议的
    # Provider 在自己内部翻译，调用方（agent loop）不感知协议差异。
    def supports_tools(self) -> bool:
        return False

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """流式对话，支持工具调用。tools 为空时等价于 stream()（文本增量包装）。

        默认实现只处理无工具场景，保证所有 Provider 都能被 agent loop 统一调用；
        带 tools 调用一个不支持工具的 Provider 是编排层的 bug——用 supports_tools()
        先分流，而不是靠这里抛错兜底。
        """
        if tools:
            raise NotImplementedError("当前模型不支持工具调用")
        async for text in self.stream(messages, temperature=temperature, max_tokens=max_tokens):
            yield StreamDelta(kind="text", text=text)

    # ── 多模态（视觉）：默认不支持，视觉 Provider 覆盖 ──
    def supports_vision(self) -> bool:
        return False

    async def complete_vision(
        self, prompt: str, images: list[tuple[str, str]], max_tokens: int | None = None,
    ) -> str:
        """据若干图片 + 文本提示生成文本（多模态）。images=[(base64, mime), …]。非视觉 Provider 不实现。"""
        raise NotImplementedError("当前模型不支持多模态")
