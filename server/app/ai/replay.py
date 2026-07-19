"""团记导出（P4 5.2b）：把整局事件流逐窗低温改写成小说体 / 剧本体 markdown。

纯离线批处理：按 token 分窗，逐窗改写，窗间携带上一窗结尾保证衔接。每窗 fail-open——
改写失败回退为该窗的朴素文本，导出始终产出完整内容，不阻塞。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TAIL_CHARS = 200                 # 携带给下一窗的上文结尾长度

_STYLE_GUIDE = {
    "novel": (
        "把这段跑团记录改写成**第三人称小说体**：连贯流畅、有文学性，"
        "保留关键情节、抉择与对话（对话可融入叙述或用引号），略去掷骰的机械细节"
        "（只体现其成败后果），不要加入原记录中没有的情节。"
    ),
    "script": (
        "把这段跑团记录改写成**剧本体**：以「场景：地点」起头（若能判断），"
        "角色台词写成「角色名：台词」，动作/环境用（圆括号）舞台提示，"
        "略去掷骰机械细节（只体现后果），不要加入原记录中没有的情节。"
    ),
}


def build_replay_messages(style: str, body: str, prev_tail: str) -> list[dict]:
    guide = _STYLE_GUIDE.get(style, _STYLE_GUIDE["novel"])
    prev = (prev_tail or "").strip()
    prev_block = (
        f"【上一段结尾（用于承接，不要重复它，从其后自然续写）】\n{prev}\n\n"
        if prev else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 团记润色师，把跑团实录改写成可读的成品文稿。"
                "只输出改写后的正文，不要解释、不要标题、不要 markdown 代码围栏。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"{guide}\n\n{prev_block}"
                f"【本段跑团记录】\n{body}\n\n请输出改写后的正文："
            ),
        },
    ]


async def rewrite_window(llm: Any, style: str, body: str, prev_tail: str) -> str | None:
    """改写一窗；失败返回 None（调用方回退朴素文本）。"""
    if llm is None or not body.strip():
        return None
    try:
        raw = await llm.complete(
            build_replay_messages(style, body, prev_tail),
            temperature=0.6,
        )
    except Exception:
        logger.exception("团记改写调用失败（该窗回退朴素文本）")
        return None
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return text or None


def tail_of(text: str) -> str:
    """取一段文本结尾若干字符，携带给下一窗做衔接。"""
    t = (text or "").strip()
    return t[-_TAIL_CHARS:]
