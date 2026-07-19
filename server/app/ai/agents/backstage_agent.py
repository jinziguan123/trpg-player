"""幕后推演（Backstage Clock）：让世界在玩家不在场时按 NPC 的动机演进。

低温 JSON 调用：输入 = 带 secrets/动机的 NPC 清单 + 未触发的 triggers + 线索台账 +
幕后游标以来的事件摘要 + 当前场景；输出 0~2 条幕后事件
``{"npc_id", "action", "affected_scene", "suggest_flags"}``。

安全约束（最重要）：幕后事件绝不直接改 flags / world_state 的剧情状态——只落库
（``visibility=["kp"]``，玩家永远不可见）+ 注入 KP 上下文；``suggest_flags`` 只是给
KP 的建议，是否 ``[SET_FLAG]`` 由 KP 在后续叙事中决定。

fail-open：LLM 异常 / 坏 JSON 时 ``infer`` 返回 None，调用方据此不落库、不动游标。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.ai.agents.base import BaseAgent
from app.ai.context import _active_flags
from app.ai.provider import LLMProvider
from app.ai.turn_planner import _extract_json_object

logger = logging.getLogger(__name__)

# 单次推演最多产出的幕后事件数（设计稿：0~2 条，一次一小步）
MAX_BACKSTAGE_EVENTS = 2
# 事件摘要最多回看的条数 / 单条截断长度（幕后推演只需要「大概发生了什么」）
DIGEST_MAX_EVENTS = 30
DIGEST_LINE_MAX_CHARS = 100
# 单条幕后事件 action 文本的截断长度
ACTION_MAX_CHARS = 200


def _as_text(v: Any) -> str:
    """secrets/goals 等字段容忍 str 或 list[str] 两种形态。"""
    if isinstance(v, list):
        return "\n".join(str(x) for x in v if str(x).strip())
    return str(v or "")


def npcs_with_secrets(module: Any) -> list[dict]:
    """模组里带 secrets / goals / motivation 的 NPC——幕后推演的主体。

    一个都没有 → 本模组永不触发幕后推演（调用方据此零调用直接跳过）。
    """
    out: list[dict] = []
    for n in (getattr(module, "npcs", None) or []):
        if not isinstance(n, dict) or not n.get("id"):
            continue
        if (
            _as_text(n.get("secrets")).strip()
            or _as_text(n.get("goals")).strip()
            or _as_text(n.get("motivation")).strip()
        ):
            out.append(n)
    return out


def _untriggered_triggers(module: Any, active_flags: set[str]) -> list[dict]:
    """尚未触发的剧情钩子（set_flags 未全部激活），附带其 set_flags 供 suggest 参考。"""
    out: list[dict] = []
    for trig in (getattr(module, "triggers", None) or []):
        if not isinstance(trig, dict):
            continue
        set_flags = trig.get("set_flags") or trig.get("flags") or []
        if isinstance(set_flags, str):
            set_flags = [set_flags]
        if set_flags and all(f in active_flags for f in set_flags):
            continue  # 已触发
        desc = str(trig.get("description") or trig.get("when") or "").strip()
        if desc:
            out.append({"when": desc, "set_flags": list(set_flags)})
    return out


def _events_digest(events: list[Any]) -> str:
    """把幕后游标以来的事件浓缩成纯文本摘要（只看玩家可见的剧情事件，去噪）。"""
    lines: list[str] = []
    for ev in events[-DIGEST_MAX_EVENTS:]:
        etype = getattr(ev, "event_type", "") or ""
        if etype not in ("narration", "dialogue", "action", "dice"):
            continue  # system（含既往幕后事件）/ooc 是噪音，不回灌
        content = (getattr(ev, "content", "") or "").replace("\n", " ").strip()
        if not content:
            continue
        who = getattr(ev, "actor_name", "") or etype
        lines.append(f"- {who}：{content[:DIGEST_LINE_MAX_CHARS]}")
    return "\n".join(lines) if lines else "（暂无）"


def build_backstage_messages(
    session: Any,
    module: Any,
    secret_npcs: list[dict],
    events_since_cursor: list[Any],
) -> list[dict]:
    """构建幕后推演的 LLM 消息（纯函数，不触数据库）。"""
    from app.services import world_memory  # 局部导入，避免与 context 的依赖交叉

    flags = _active_flags(session)
    ws = session.world_state or {}
    scenes = [
        {"id": s.get("id", ""), "name": s.get("name") or s.get("title") or ""}
        for s in (getattr(module, "scenes", None) or [])
    ]
    payload = {
        "current_scene_id": session.current_scene_id,
        # 幕后真相：世界演进的总纲——NPC 的小步动作应与全局真相/时间线相符
        "truth": (getattr(module, "truth", "") or "").strip(),
        "scenes": scenes,
        "npcs": [
            {
                "id": n.get("id", ""),
                "name": n.get("name", ""),
                "personality": (n.get("personality") or "")[:80],
                "secrets": _as_text(n.get("secrets"))[:200],
                "goals": _as_text(n.get("goals") or n.get("motivation"))[:200],
                "location": n.get("initial_location") or "",
            }
            for n in secret_npcs
        ],
        "untriggered_triggers": _untriggered_triggers(module, flags),
        "active_flags": sorted(flags),
        "clue_ledger": world_memory.discovered_clue_status(ws),
        "recent_events": _events_digest(events_since_cursor),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 的幕后推演器（Backstage Clock）。玩家看不见的地方，"
                "世界仍在按各 NPC 的秘密与动机运转。请基于下面的资料，推演自上次推演以来，"
                "NPC 们在玩家视野之外**最可能悄悄做了什么**，输出 0~2 条幕后事件。\n"
                '只输出一个 JSON object，形如：{"events":[{"npc_id":"npc_x",'
                '"action":"把尸体从地窖移到井里","affected_scene":"scene_well",'
                '"suggest_flags":["flag_body_moved"]}]}，不要输出 Markdown。\n'
                "要求：\n"
                "1. 动作必须符合该 NPC 的秘密/动机与当前局势，幅度克制——一次只走一小步，"
                "不要一步掀翻全局，也不要凭空引入模组里不存在的人物或超展开。\n"
                "2. npc_id 必须取自资料里的 NPC 清单；affected_scene 尽量取 scenes 里的场景 id。\n"
                "3. suggest_flags 只是「若剧情落实可考虑推进的标志」建议（优先取 "
                "untriggered_triggers 里已有的 set_flags），世界状态不会因此直接改变。\n"
                "4. 没有值得发生的事就返回 {\"events\":[]}——宁缺毋滥。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def parse_backstage_events(raw: Any, valid_npc_ids: set[str]) -> list[dict] | None:
    """解析幕后推演输出。

    坏 JSON → None（调用方视为本次失败：游标不动、不落库）；
    合法 JSON 但无有效事件 → []（推演过了、无事发生，游标照常推进）。
    npc_id 不在模组内 / action 为空的条目视为幻觉丢弃；最多保留 2 条。
    """
    data = _extract_json_object(raw)
    if data is None:
        return None
    items = data.get("events")
    if isinstance(items, dict):
        items = [items]
    if items is None:
        # 模型直接输出了单条事件对象（无 events 包裹）也宽容接受
        items = [data] if data.get("npc_id") else []
    if not isinstance(items, list):
        return None
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        npc_id = str(it.get("npc_id") or "").strip()
        action = str(it.get("action") or "").strip()
        if not action or npc_id not in valid_npc_ids:
            continue
        raw_flags = it.get("suggest_flags")
        flags = (
            [str(f).strip() for f in raw_flags if str(f).strip()]
            if isinstance(raw_flags, list) else []
        )
        out.append({
            "npc_id": npc_id,
            "action": action[:ACTION_MAX_CHARS],
            "affected_scene": str(it.get("affected_scene") or "").strip(),
            "suggest_flags": flags[:4],
        })
        if len(out) >= MAX_BACKSTAGE_EVENTS:
            break
    return out


class BackstageAgent(BaseAgent):
    """低温（0.2）JSON 推演器：产出幕后事件列表，失败返回 None（fail-open）。"""

    def __init__(self, llm: LLMProvider):
        super().__init__(llm, temperature=0.2)

    async def infer(
        self, messages: list[dict], valid_npc_ids: set[str],
    ) -> list[dict] | None:
        try:
            raw = await self.llm.complete(
                messages,
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
        except Exception:
            logger.exception("幕后推演调用失败（忽略，游标不动）")
            return None
        events = parse_backstage_events(raw, valid_npc_ids)
        if events is None:
            logger.warning("幕后推演输出无法解析为 JSON（忽略，游标不动）：%s", str(raw)[:200])
        return events
