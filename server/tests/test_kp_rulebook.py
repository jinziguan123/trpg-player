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


def test_rulebook_static_leaves_budget_for_module_data():
    static = re.sub(r"\{[a-z_]+\}", "", KP_SYSTEM_PROMPT)
    static_tokens = _estimate_tokens(static)
    # 静态提示词不得吃掉全部预算，至少给模组数据留 ~2000 token 余量
    assert static_tokens < MAX_SYSTEM_TOKENS - 2000, (
        f"KP 系统提示词过长（{static_tokens} token），模组数据预算不足"
    )
