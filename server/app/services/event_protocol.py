"""房间事件与 SSE chunk 的传输协议。"""

from __future__ import annotations

import json
import re


OOC_RE = re.compile(r"（[^（）]*）|\([^()]*\)")
QUOTE_RE = re.compile(r'[“"「『]([^”"」』]*)[”"」』]')


def split_speech_action(text: str) -> list[tuple[str, str]]:
    """按引号约定把玩家输入拆成有序的行动与台词。"""
    segments: list[tuple[str, str]] = []
    last = 0
    for match in QUOTE_RE.finditer(text or ""):
        before = (text[last : match.start()] or "").strip(" \t\n，,。.、")
        if before:
            segments.append(("action", before))
        inner = (match.group(1) or "").strip()
        if inner:
            segments.append(("dialogue", inner))
        last = match.end()
    tail = (text[last:] if text else "").strip(" \t\n，,。.、")
    if tail:
        segments.append(("action", tail))
    return segments


def split_ooc(text: str) -> tuple[str, str]:
    """拆出正式行动与 OOC 内容，返回 ``(in_character, ooc)``。"""
    ooc_parts = OOC_RE.findall(text or "")
    in_character = OOC_RE.sub("", text or "").strip()
    ooc = " ".join(part[1:-1].strip() for part in ooc_parts if len(part) >= 2).strip()
    return in_character, ooc


def make_chunk(
    chunk_type: str,
    content: str = "",
    actor_name: str | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
    actor_id: str | None = None,
) -> str:
    """把房间消息编码为 SSE data chunk。"""
    data: dict = {"type": chunk_type, "content": content}
    if actor_name:
        data["actor_name"] = actor_name
    if metadata:
        data["metadata"] = metadata
    if event_id:
        data["id"] = event_id
    if actor_id:
        data["actor_id"] = actor_id
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def event_to_chunk(event) -> str:
    """把持久 EventLog 序列化为 `/live` 重放用的 chunk。"""
    type_map = {
        "dialogue": "dialogue",
        "action": "action",
        "dice": "dice",
        "narration": "narration_full",
        "system": "system",
        "ooc": "ooc",
    }
    return make_chunk(
        type_map.get(event.event_type, event.event_type),
        event.content,
        actor_name=event.actor_name or None,
        metadata=event.metadata_ or None,
        event_id=event.id,
        actor_id=event.actor_id,
    )
