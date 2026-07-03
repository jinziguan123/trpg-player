"""KP 裁定手册的轻量守卫测试。

不验证 LLM 行为，只守住两条底线：
1. 系统提示词里包含核心裁定手册各分区（信息分层 + 检定先行纪律必须在场）。
2. 渲染后的静态系统提示词留给模组数据的预算足够，防止后续 prompt 膨胀
   把 MAX_SYSTEM_TOKENS 顶满导致尾部（玩家信息/约束）被截断。
"""

import re

from app.ai.context import _estimate_tokens, MAX_SYSTEM_TOKENS
from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT


def test_rulebook_sections_present():
    s = KP_SYSTEM_PROMPT
    for marker in (
        "技能检定流程",
        "属性（特征）检定",
        "调查与信息揭示纪律",
        "检定先行",
        "信息分层",
        "理智检定",
        "战斗",
        "伤害、濒死与急救",
        "常见情境裁定",
        "孤注一掷",
    ):
        assert marker in s, f"裁定手册缺少分区：{marker}"


def test_characteristic_checks_and_proactive_guidance():
    """属性检定入册 + KP 主动推进指引：九维属性可作 [DICE_CHECK] 的 skill、
    典型用途映射在册；卡关/需专业知识时 KP 应主动发起灵感/教育检定，不干等玩家申请。"""
    s = KP_SYSTEM_PROMPT
    # 九维属性典型用途映射（中文名直接作 skill）
    for kw in ("力量", "体质", "敏捷", "外貌", "意志", "灵感", "知识", "幸运"):
        assert kw in s, f"属性检定小节缺少属性：{kw}"
    assert "系统自动按属性值判定" in s
    # 主动推进：困在原地/多轮无进展/需专业知识 → KP 主动发起检定
    assert "应当主动" in s
    assert "不要干等玩家想起来申请" in s


def test_rulebook_static_leaves_budget_for_module_data():
    static = re.sub(r"\{[a-z_]+\}", "", KP_SYSTEM_PROMPT)
    static_tokens = _estimate_tokens(static)
    # 静态提示只是系统内容的一部分；.format() 后还要接模组数据 + RAG 原文摘录 + 线索台账 +
    # NPC 记忆 + 幕后动态 + handout 清单（P1-P3 陆续加入，合计可达数千 token）。MAX_SYSTEM_TOKENS
    # 放宽到 12000 后，仍守住静态提示不膨胀：给下游注入内容留足 ~6000 token 余量。
    assert static_tokens < MAX_SYSTEM_TOKENS - 6000, (
        f"KP 系统提示词过长（{static_tokens} token），下游注入内容预算不足"
    )
