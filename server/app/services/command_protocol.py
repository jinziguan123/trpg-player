"""KP 控制标签协议。

该模块只定义模型输出中的控制标签、解析规则和单轮配额，不执行任何业务副作用。
叙事过滤器与工具执行器共同依赖这里，避免各自维护一套宽容解析逻辑。
"""

from __future__ import annotations

import re


# 骰子与规则工具标签。
DICE_CHECK_RE = re.compile(r"\[DICE_CHECK:([^\]]*)\]")
OPPOSED_CHECK_RE = re.compile(r"\[OPPOSED_CHECK:([^\]]*)\]")
SAN_CHECK_RE = re.compile(r"\[SAN_CHECK:([^\]]*)\]")
HP_CHANGE_RE = re.compile(
    r"\[HP_CHANGE:\s*target=([^,\]]+),?\s*delta=([^,\]]+),?\s*reason=([^\]]*)\]"
)
NPC_ACT_RE = re.compile(
    r"\[NPC_ACT:\s*npc_id=([^,\]]+),?\s*trigger=([^\]]+)\]"
)
SCENE_CHANGE_RE = re.compile(
    r"\[SCENE_CHANGE:\s*(?:scene_id=)?([^\]]+)\]"
)
RULE_LOOKUP_RE = re.compile(r"\[RULE_LOOKUP:\s*query=([^\]]+)\]")
MODULE_LOOKUP_RE = re.compile(r"\[MODULE_LOOKUP:\s*query=([^\]]+)\]")
SET_FLAG_RE = re.compile(r"\[SET_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
CLEAR_FLAG_RE = re.compile(r"\[CLEAR_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
HANDOUT_RE = re.compile(r"\[HANDOUT[:：\s]\s*([^\]]+?)\s*\]")
GROUP_RE = re.compile(r"\[GROUP:([^\]]*)\]")

CMD_TAG_PREFIXES = (
    "DICE_CHECK:",
    "OPPOSED_CHECK:",
    "SAN_CHECK:",
    "HP_CHANGE:",
    "NPC_ACT:",
    "SCENE_CHANGE:",
    "RULE_LOOKUP:",
    "MODULE_LOOKUP:",
    "SET_FLAG:",
    "CLEAR_FLAG:",
    "HANDOUT:",
)
_CMD_TAG_KEYWORDS = tuple(prefix.rstrip(":") for prefix in CMD_TAG_PREFIXES)

# 规则书与模组原文共享单轮查阅配额；骰子续写另有独立深度限制。
MAX_RULE_LOOKUPS = 2
MAX_DICE_CONTINUATIONS = 3


def is_command_tag(inner: str) -> bool:
    """判断去掉括号后的文本是否为终止型指令，容忍缺冒号或使用空格。"""
    value = inner.lstrip()
    return any(
        value == keyword
        or value.startswith(keyword + ":")
        or value.startswith(keyword + "：")
        or value.startswith(keyword + " ")
        for keyword in _CMD_TAG_KEYWORDS
    )


def parse_tag_kv(inner: str) -> dict[str, str]:
    """解析 ``a=x, b=y`` 形式的指令参数。"""
    result: dict[str, str] = {}
    for part in (inner or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result
