"""各厂商 LLM Provider 实现（均实现 app.ai.provider.LLMProvider 抽象接口）。

- OpenAICompatProvider：OpenAI 兼容协议（DeepSeek / OpenAI / 兼容端点）。
- AnthropicProvider：Anthropic Messages API（协议与 OpenAI 兼容协议根本不同，故独立成文件）。

选择哪个 Provider 由 app.ai.llm_factory.get_llm() 按激活配置的 protocol 决定。
"""

from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.openai_compat import OpenAICompatProvider

__all__ = ["AnthropicProvider", "OpenAICompatProvider"]
