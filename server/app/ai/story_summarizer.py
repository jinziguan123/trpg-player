from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 单次滚动摘要的输出上限（token 粗上限）：控制摘要不随游戏无限膨胀。
MAX_STORY_SUMMARY_TOKENS = 1200


def _events_text(events: list[Any]) -> str:
    """把一批事件渲染成给「剧情书记员」浓缩用的纯文本（客观、去格式）。"""
    lines: list[str] = []
    for ev in events:
        etype = getattr(ev, "event_type", "") or ""
        who = getattr(ev, "actor_name", "") or ""
        content = (getattr(ev, "content", "") or "").replace("\n", " ").strip()
        if not content:
            continue
        if etype == "narration":
            lines.append(f"旁白：{content}")
        elif etype == "dialogue":
            lines.append(f"{who or 'NPC'}：{content}")
        elif etype == "action":
            lines.append(f"{who or '某人'}（行动）：{content}")
        elif etype == "dice":
            lines.append(f"检定：{content}")
        elif etype == "system":
            lines.append(f"系统：{content}")
    return "\n".join(lines)


def build_summary_messages(prev_summary: str, events: list[Any]) -> list[dict]:
    body = _events_text(events)
    prev = (prev_summary or "").strip() or "（暂无既往摘要）"
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 剧情书记员。把既往剧情摘要与新发生的一段事件，合并浓缩成一份**连贯、"
                "客观**的剧情进展梗概，供 KP 后续叙事时回顾。只输出梗概正文，不要解释、不要标题。"
            ),
        },
        {
            "role": "user",
            "content": (
                "要求：\n"
                "- 保留对后续推进重要的事实：去过哪些地点、见过哪些 NPC、已揭示的线索与结论、"
                "已做出的关键决定与承诺、尚未了结的悬念/待办、当前处境。\n"
                "- 舍弃寒暄与重复的氛围描写；按时间顺序、紧凑成段。\n"
                "- 以既往摘要为基础做增量更新，不要丢失其中仍然重要的内容。\n\n"
                f"【既往剧情摘要】\n{prev}\n\n【新发生的事件】\n{body}\n\n"
                "请输出更新后的完整剧情梗概："
            ),
        },
    ]


async def summarize_story(llm: Any, prev_summary: str, events: list[Any]) -> str | None:
    """把既往摘要 + 新事件合并成新的滚动剧情摘要。

    失败（无 LLM / 无事件 / 调用异常 / 产出为空）一律返回 None，由调用方保持原摘要不变，
    绝不阻塞跑团。
    """
    if llm is None or not events:
        return None
    try:
        raw = await llm.complete(
            build_summary_messages(prev_summary, events),
            temperature=0.2,
            max_tokens=MAX_STORY_SUMMARY_TOKENS,
        )
    except Exception:
        logger.exception("滚动剧情摘要生成失败，保持原摘要")
        return None
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return text or None
