"""LLM Provider 工厂：按激活配置创建对应厂商的 Provider。

与具体厂商解耦——各 Provider 实现在 app.ai.providers 下，本文件只负责「按配置选谁」。
为兼容既有导入，仍从此处再导出 OpenAICompatProvider。
"""

from app.ai.provider import LLMProvider
from app.ai.providers.openai_compat import OpenAICompatProvider

__all__ = ["get_llm", "OpenAICompatProvider"]


def get_llm() -> LLMProvider:
    """根据激活的配置创建对应的 LLM Provider。

    - protocol="anthropic" -> AnthropicProvider
    - protocol="openai"（默认）-> OpenAICompatProvider（兼容 OpenAI API）
    - 没有激活配置时，回退到 .env 环境变量
    """
    from app.api.ai_settings import load_active_profile

    profile = load_active_profile()

    if profile:
        if profile.protocol == "anthropic":
            from app.ai.providers.anthropic import AnthropicProvider
            return AnthropicProvider(
                model=profile.model_name,
                base_url=profile.base_url,
                api_key=profile.api_key,
            )
        return OpenAICompatProvider(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=profile.api_key,
            vision=getattr(profile, "vision", False),
        )

    # 没有激活配置，回退到 .env 配置
    from app.config import settings
    return OpenAICompatProvider(
        model="deepseek-chat",
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
    )
