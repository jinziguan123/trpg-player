"""战报 recap（P4 5.2a）：会话结束 / 章节小结时，把整局浓缩成一份结构化战报——
关键抉择、已解/未解线索、名场面引用（带事件 seq）、阵亡与损失。

低温 JSON 调用，fail-open：任何失败（无 LLM / 无事件 / 坏 JSON / 空标题）返回 None，
调用方不落库、不阻塞。复用 story_summarizer 的事件渲染与 JSON 抠取。
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai.story_summarizer import _events_text, _extract_json_object

logger = logging.getLogger(__name__)

MAX_RECAP_TOKENS = 1500
# 喂给战报的事件正文字符上限：整局可能很长，截断到尾部（近段更关键），配合滚动摘要覆盖前情。
_EVENTS_CHAR_BUDGET = 8000


def _as_str_list(v: Any, limit: int = 12) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if str(x).strip()]
    return out[:limit]


def _as_highlights(v: Any, limit: int = 6) -> list[dict]:
    """名场面引用：[{seq:int, quote:str}]，容忍缺字段/脏数据。"""
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for item in v:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        seq = item.get("seq")
        out.append({"seq": int(seq) if isinstance(seq, (int, float)) else None, "quote": quote})
        if len(out) >= limit:
            break
    return out


def build_recap_messages(
    prev_summary: str, events: list[Any], clue_ledger_text: str, party_status_text: str,
) -> list[dict]:
    body = _events_text(events)
    if len(body) > _EVENTS_CHAR_BUDGET:
        body = "……（前略）\n" + body[-_EVENTS_CHAR_BUDGET:]
    prev = (prev_summary or "").strip() or "（无既往摘要）"
    ledger = (clue_ledger_text or "").strip() or "（无线索台账）"
    party = (party_status_text or "").strip() or "（无角色状态）"
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 战报撰稿人。把这一局（或本章节）的经历浓缩成一份**结构化战报**，"
                "客观、扣事实，供玩家赛后回顾。**只输出一个 JSON 对象**，不要解释、不要 markdown 围栏。"
            ),
        },
        {
            "role": "user",
            "content": (
                "字段要求：\n"
                "- title：一句话章节/战报标题（不剧透未解之谜，扣本局主线）。\n"
                "- key_decisions：玩家做出的关键抉择与转折（数组，各一句话，最多 ~8 条）。\n"
                "- clues_resolved：已查明/已了结的线索或谜团（据线索台账与剧情，数组）。\n"
                "- clues_unresolved：尚未了结的悬念/待办（数组）。\n"
                "- highlights：名场面引用，数组，每项 {seq: 事件序号(整数,尽量取自事件), quote: 一句原话/名场面简述}。\n"
                "- casualties：阵亡、重伤、疯狂、重大损失（据角色状态与剧情，数组；无则空数组）。\n"
                "只依据给定材料，绝不臆造未发生的情节。\n\n"
                f"【剧情梗概】\n{prev}\n\n【线索台账】\n{ledger}\n\n【角色当前状态】\n{party}\n\n"
                f"【本局事件（节选）】\n{body}\n\n"
                '现在输出 JSON：{"title":"","key_decisions":[],"clues_resolved":[],'
                '"clues_unresolved":[],"highlights":[],"casualties":[]}'
            ),
        },
    ]


async def generate_recap(
    llm: Any, *, prev_summary: str, events: list[Any],
    clue_ledger_text: str = "", party_status_text: str = "",
) -> dict | None:
    """产出结构化战报 dict；失败返回 None（调用方不落库、不阻塞）。"""
    if llm is None or not events:
        return None
    try:
        raw = await llm.complete(
            build_recap_messages(prev_summary, events, clue_ledger_text, party_status_text),
            temperature=0.3,
            max_tokens=MAX_RECAP_TOKENS,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("战报生成调用失败")
        return None
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "key_decisions": _as_str_list(data.get("key_decisions")),
        "clues_resolved": _as_str_list(data.get("clues_resolved")),
        "clues_unresolved": _as_str_list(data.get("clues_unresolved")),
        "highlights": _as_highlights(data.get("highlights")),
        "casualties": _as_str_list(data.get("casualties")),
    }
