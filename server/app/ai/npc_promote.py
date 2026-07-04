"""临场 NPC 受控转正（P2）：把一个出彩的临场龙套，据其既有台词与相关事件，
生成一张完整的会话级 NPC 卡（性格/立场/合理知道的边界）。

低温 JSON 调用，fail-open：任何失败（无 LLM / 无素材 / 坏 JSON / 空名）返回 None，
调用方不落库、不阻塞。secrets 恒为空——转正不自动获得秘密，秘密仍属模组。
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai.story_summarizer import _events_text, _extract_json_object

logger = logging.getLogger(__name__)

MAX_PROMOTE_TOKENS = 900
_EVENTS_CHAR_BUDGET = 6000


def collect_npc_material(events: list[Any], name: str) -> str:
    """收集与该临场 NPC 相关的素材：他的全部台词/行动，外加提到其名字的旁白。

    只取与他直接相关的事件，避免把整局无关剧情灌进转正 prompt。"""
    name = (name or "").strip()
    if not name:
        return ""
    picked: list[Any] = []
    for ev in events or []:
        who = (getattr(ev, "actor_name", "") or "").strip()
        content = getattr(ev, "content", "") or ""
        etype = getattr(ev, "event_type", "") or ""
        if who == name and etype in ("dialogue", "action"):
            picked.append(ev)
        elif etype == "narration" and name in content:
            picked.append(ev)
    body = _events_text(picked)
    if len(body) > _EVENTS_CHAR_BUDGET:
        body = "……（前略）\n" + body[-_EVENTS_CHAR_BUDGET:]
    return body


def build_promote_messages(name: str, material: str, module_title: str) -> list[dict]:
    material = (material or "").strip() or "（该角色目前只有零星登场，请据其登场情境合理补全人设）"
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 的角色设定师。守秘人在跑团中临时添加了一个龙套，如今要把他/她"
                "**转正为正式配角**。请**仅依据他既有的言行**，为他生成一张自洽的 NPC 卡。"
                "**只输出一个 JSON 对象**，不要解释、不要 markdown 围栏。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"模组：《{module_title}》。要转正的角色名：{name}。\n"
                "字段要求：\n"
                "- name：沿用上面的角色名，原样输出。\n"
                "- description：外貌与身份的客观速写（一两句，扣既有言行，不拔高其重要性）。\n"
                "- personality：性格与说话风格（一句）。\n"
                "- background：与其身份相称的**日常**背景（一两句）。**不得**赋予他任何案情内幕、"
                "隐藏真相或线索级信息——他仍是配角，剧情秘密只属模组。\n"
                "只依据给定言行，绝不臆造他掌握的情报或秘密。\n\n"
                f"【{name} 的既有言行】\n{material}\n\n"
                '现在输出 JSON：{"name":"","description":"","personality":"","background":""}'
            ),
        },
    ]


async def generate_npc_card(
    llm: Any, *, name: str, material: str, module_title: str = "",
) -> dict | None:
    """产出转正 NPC 卡 dict（secrets 恒空）；失败返回 None。"""
    name = (name or "").strip()
    if llm is None or not name:
        return None
    try:
        raw = await llm.complete(
            build_promote_messages(name, material, module_title),
            temperature=0.3,
            max_tokens=MAX_PROMOTE_TOKENS,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("临场 NPC 转正生成调用失败：name=%s", name)
        return None
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        return None
    return {
        # name 强制用传入值，避免模型改名导致与 improvised_npcs 的 key 对不上
        "name": name,
        "description": str(data.get("description") or "").strip(),
        "personality": str(data.get("personality") or "").strip(),
        "background": str(data.get("background") or "").strip(),
        "secrets": [],   # 转正不自动获得秘密——秘密仍属模组
    }
