"""LLM Provider 工厂：按激活配置创建对应厂商的 Provider。

与具体厂商解耦——各 Provider 实现在 app.ai.providers 下，本文件只负责「按配置选谁」。
为兼容既有导入，仍从此处再导出 OpenAICompatProvider。
"""

from app.ai.provider import LLMProvider
from app.ai.providers.openai_compat import OpenAICompatProvider

__all__ = ["get_llm", "get_fast_llm", "OpenAICompatProvider"]


def _provider_from_profile(profile) -> LLMProvider:
    if profile.protocol == "anthropic":
        from app.ai.providers.anthropic import AnthropicProvider
        provider: LLMProvider = AnthropicProvider(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=profile.api_key,
        )
    else:
        provider = OpenAICompatProvider(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=profile.api_key,
            vision=getattr(profile, "vision", False),
            reasoning_effort=getattr(profile, "reasoning_effort", ""),
            image_model=getattr(profile, "image_model", ""),
            image_base_url=getattr(profile, "image_base_url", ""),
            image_api_key=getattr(profile, "image_api_key", ""),
        )
    # 图片后端选 ComfyUI 时挂上客户端：任何协议的文本模型都获得文生图能力
    if (
        getattr(profile, "image_backend", "") == "comfyui"
        and getattr(profile, "comfyui_base_url", "").strip()
    ):
        from app.ai.comfyui import ComfyUIClient
        provider.set_comfyui(ComfyUIClient(
            profile.comfyui_base_url.strip(),
            getattr(profile, "comfyui_workflow", ""),
        ))
    return provider


def provider_from_profile(profile) -> LLMProvider:
    """公开入口：按任意 profile 建 Provider（设置页测试连接/测试生图用，保证与运行时同一装配）。"""
    return _provider_from_profile(profile)


def get_llm() -> LLMProvider:
    """根据激活的配置创建对应的 LLM Provider。

    - protocol="anthropic" -> AnthropicProvider
    - protocol="openai"（默认）-> OpenAICompatProvider（兼容 OpenAI API）

    AI 配置的唯一真源是设置页（ai_settings.json 的激活 profile）；不再有 .env 回退。
    没有激活配置时抛出可读错误，由生成路径兜成「请到设置页配置 AI」的提示。
    """
    from app.api.ai_settings import load_active_profile

    profile = load_active_profile()
    if not profile:
        raise ValueError("未配置可用的 AI 模型：请到设置页添加并激活一个 AI 配置。")
    return _provider_from_profile(profile)


def get_fast_llm() -> LLMProvider:
    """结构化副任务（planner / AI 队友 / 滚动摘要 / 幕后推演）用的「快模型」。

    设置页可把某个配置标记为快模型（is_fast）；未标记时回落到主模型（行为与从前一致）。
    KP 主叙事与 NPC 台词**永远走主模型**——快模型只接不直面玩家文笔的结构化任务。
    """
    from app.api.ai_settings import load_fast_profile

    profile = load_fast_profile()
    if not profile:
        return get_llm()
    return _provider_from_profile(profile)
