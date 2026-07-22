"""AI 队友回合决策、行动落库与暗骰处理。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai.agents.kp_agent import KPAgent
from app.ai.agents.team_agent import TeamAgent
from app.ai.context import build_team_context
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import dice_runtime, narration_protocol, session_service, turn_context, world_memory
from app.services.event_protocol import make_chunk

_make_chunk = make_chunk
_filter_narration_stream = narration_protocol.filter_narration_stream
_resolve_scene_ref = turn_context._resolve_scene_ref
_scene_name = turn_context._scene_name
_apply_world_memory = turn_context._apply_world_memory
_match_single_npc = turn_context._match_single_npc
_check_dice_detail = dice_runtime._check_dice_detail
ALWAYS_BLIND_SKILLS = dice_runtime.ALWAYS_BLIND_SKILLS
TIER_LABEL = dice_runtime.TIER_LABEL

logger = logging.getLogger(__name__)

def _matcher_npcs(
    module: Module,
    teammates: list[Character] | None,
    session: GameSession | None = None,
) -> list[dict]:
    """供行内台词归属用的名字表：模组 NPC + 已转正/已登记的临场 NPC + 在场队友（真人/AI）。

    队友不在 module.npcs 里，若不加进来，KP 偶尔替队友写的引号台词会被
    错误归给附近提到的某个模组 NPC（如把约翰·卡特的话记到萨沙·卡纳头上）。
    已转正的临场 NPC 同理并入。**已登记但未转正的临场龙套（管理员/护士长…）也并入**：
    否则它们的台词无名可归，会被在名字表里的某个模组 NPC（甚至已死反派）劫走——即
    「管理员的话被记到沃尔特·科比特头上」。只并入通过合理性校验的名字（滤掉旁白碎片）。
    """
    extra = [{"name": t.name, "is_player": True} for t in (teammates or []) if t.name]
    promoted = world_memory.promoted_npc_cards(session.world_state or {}) if session else []
    improv: list[dict] = []
    if session:
        promoted_names = {c.get("name") for c in promoted}
        for name in (session.world_state or {}).get("improvised_npcs") or {}:
            name = str(name).strip()
            if (name and name not in promoted_names
                    and world_memory.is_plausible_npc_name(name)):
                improv.append({"name": name})
    return (module.npcs or []) + promoted + improv + extra




async def _stream_narration_filtered(
    kp: KPAgent, messages: list[dict], result: list,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    party_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """旧路径入口：KPAgent 流式生成 + 台词过滤（核心逻辑在 _filter_narration_stream）。"""
    async for chunk in _filter_narration_stream(
        kp.narrate(messages), result, npcs=npcs, group_label=group_label,
        party_names=party_names,
    ):
        yield chunk



MAX_TEAMMATES_PER_TURN = 4

# check：队友主动发起的技能检定（content 描述尝试、skill 给技能），先落 action 事件再掷骰。
TEAM_ACTION_EVENT = {
    "speak": "dialogue", "act": "action", "assist": "action", "check": "action",
}


def _parse_team_decision(raw) -> dict | None:
    """解析队友 agent 的 JSON 决策；失败返回 None（编排层据此 hold）。"""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return None
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    else:
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in TEAM_ACTION_EVENT and action not in ("silent", "travel"):
        return None
    return {
        "action": action,
        "content": str(data.get("content") or "").strip(),
        "skill": str(data.get("skill") or "").strip(),   # 仅 action=check 时有意义
        "target": str(data.get("target") or "").strip(),  # 仅 action=travel 时有意义
    }


async def _run_team_turn(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    teammates: list[Character],
    llm,
    blind_results: list[str] | None = None,
    team_guidance: str = "",
) -> AsyncIterator[str]:
    """玩家输入后的一轮 AI 队友自动响应。

    每个队友只决策一次；决策**并发执行**（同一份事件快照，N 次串行调用变 1 次墙钟时间——
    代价是同轮队友互相看不到彼此这轮刚说的话，撞话题的偶发风险换整体延迟），结果按席位
    顺序写入事件流。本函数只由 ``run_chat_generation`` / ``run_travel_generation`` 调用，
    不会自触发，故不存在递归链式生成。

    分头判定：队友所在场景 ≠ 主队锚点场景（主角所在）即视为「分头独处」，据此让
    ``build_team_context`` 下达「主动推进本场景」指引；同处一地仍是克制补位。

    ``blind_results``：队友做「始终暗投」技能（如心理学）检定时，真实成败只 append 到这里、
    由调用方注入当轮 KP 上下文，绝不落库/广播——否则玩家能从事件或网络看到结果而元游戏。
    """
    roster = teammates[:MAX_TEAMMATES_PER_TURN]
    if not roster:
        return
    yield _make_chunk("housekeeping", "队友们正在思考…")
    anchor_scene = (
        session_service.get_char_location(game_session, player_char.id)
        or game_session.current_scene_id
    )
    events = session_service.get_session_events(db, session_id)

    async def _decide(teammate: Character) -> str | None:
        tm_scene = (
            session_service.get_char_location(game_session, teammate.id)
            or game_session.current_scene_id
        )
        separated = bool(tm_scene and anchor_scene and tm_scene != anchor_scene)
        messages = build_team_context(
            teammate, game_session, module, events, player_char,
            all_teammates=teammates, separated=separated,
            team_guidance=team_guidance,
        )
        t0 = time.monotonic()
        try:
            raw = await TeamAgent(llm, teammate.id).decide(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("队友决策失败: char=%s", teammate.id)
            return None
        logger.info(
            "耗时|队友决策 %.1fs char=%s(%s)",
            time.monotonic() - t0, teammate.name, teammate.id,
        )
        return raw

    raws = await asyncio.gather(*(_decide(t) for t in roster))
    for teammate, raw in zip(roster, raws):
        if raw is None:
            continue
        decision = _parse_team_decision(raw)
        if not decision:
            continue  # 解析失败：hold，不重试不递归
        action = decision["action"]
        content = decision["content"]
        # 队友「前往」：显式移动，确定性切换其所在场景（仅限已知地点），落一条「前往」事件。
        # 队友的移动由此动作触发，KP 不再从其台词臆测搬人；分头分组据 party_locations 归并。
        if action == "travel":
            sid = _resolve_scene_ref(module, decision.get("target") or content)
            known = session_service.known_scene_ids(module, game_session, events)
            cur = session_service.get_char_location(game_session, teammate.id)
            # 连通校验与玩家 travel 同规则：不连通的目标不搬（模组没建图时恒可达）
            if (
                sid and sid in known and sid != cur
                and session_service.find_scene_path(module, cur, sid) is not None
            ):
                session_service.set_char_location(db, session_id, teammate.id, sid)
                db.refresh(game_session)
                label = _scene_name(module, sid)
                ev = session_service.add_event(
                    db, session_id, "action", f"（前往：{label}）",
                    actor_id=teammate.id, actor_name=teammate.name,
                )
                yield _make_chunk(
                    "action", f"（前往：{label}）", actor_name=teammate.name,
                    event_id=ev.id, actor_id=teammate.id,
                )
                # 私有记忆钩子：自己的移动落 deeds（确定性、零 LLM）
                _apply_world_memory(
                    db, game_session,
                    lambda ws, _tid=teammate.id, _q=ev.sequence_num, _s=f"前往了{label}":
                        world_memory.record_team_deed(ws, _tid, _q, _s),
                )
            continue
        if action == "silent" or not content:
            continue
        event_type = TEAM_ACTION_EVENT[action]
        ev = session_service.add_event(
            db, session_id, event_type, content,
            actor_id=teammate.id, actor_name=teammate.name,
        )
        # speak 用 npc_dialogue 走前端气泡渲染；act/assist/check 走通用 action 渲染
        chunk_type = "npc_dialogue" if event_type == "dialogue" else "action"
        yield _make_chunk(
            chunk_type, content, actor_name=teammate.name,
            event_id=ev.id, actor_id=teammate.id,
        )
        # 私有记忆钩子：自己的言行落 deeds（确定性、零 LLM）。check 在掷骰后带结果另记。
        if action != "check":
            verb = "说" if event_type == "dialogue" else "做"
            _apply_world_memory(
                db, game_session,
                lambda ws, _tid=teammate.id, _q=ev.sequence_num, _s=f"{verb}：{content}":
                    world_memory.record_team_deed(ws, _tid, _q, _s),
            )
        # 队友主动检定：紧接着掷骰，结果落库交由 KP 收束叙述。心理学等「始终暗投」技能只落
        # 「做了一次暗骰」的事实、结果仅回灌 KP（经 blind_results 注入当轮上下文），绝不落库/
        # 广播成败——否则玩家能从事件或网络看到结果而元游戏。
        if action == "check" and decision.get("skill"):
            skill = decision["skill"]
            engine = get_engine(module.rule_system)
            cdata = {
                "base_attributes": teammate.base_attributes,
                "skills": teammate.skills,
                "system_data": teammate.system_data,
            }
            result = engine.resolve_check(cdata, skill, "normal")
            if any(s in skill for s in ALWAYS_BLIND_SKILLS):
                tier_cn = TIER_LABEL.get(result.tier, result.tier)
                dice_content = f"{teammate.name} 进行了一次暗骰·{skill}（结果仅 KP 可见）"
                dice_meta = {"skill": skill, "actor": teammate.name, "blind": True}
                if blind_results is not None:
                    blind_results.append(
                        f"【暗骰·{teammate.name}·{skill}（结果仅你 KP 可见，绝不可把成败直接告诉玩家）】"
                        f"达成 {tier_cn}：{result.description}"
                    )
            else:
                dice_content = f"{teammate.name}｜{skill} 检定（normal）：{result.description}"
                dice_meta = {
                    "skill": skill, "skill_value": result.skill_value,
                    "roll": result.roll, "target": result.target,
                    "outcome": result.outcome, "actor": teammate.name,
                    "dice": _check_dice_detail(result),
                }
            dev = session_service.add_event(
                db, session_id, "dice", dice_content,
                actor_name="系统", metadata=dice_meta,
            )
            yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=dev.id)
            # 私有记忆钩子：检定连同结果落 deeds。暗骰不落成败——team_memory 会注入队友
            # 自身上下文，落了成败就可能经其言行外泄（与 blind_results 的守密边界一致）。
            deed = (
                f"做：{content}（暗骰·{skill}）"
                if dice_meta.get("blind")
                else f"做：{content}（{skill}检定：{TIER_LABEL.get(result.tier, result.tier)}）"
            )
            _apply_world_memory(
                db, game_session,
                lambda ws, _tid=teammate.id, _q=dev.sequence_num, _s=deed:
                    world_memory.record_team_deed(ws, _tid, _q, _s),
            )
            # 世界记忆钩子 d：队友暗投若能确定性归属到唯一 NPC（行动描述里恰好点名一个），
            # 记录该 NPC「被看穿/未被看穿」；归属不成立则跳过，绝不猜测。
            if dice_meta.get("blind"):
                target = _match_single_npc(module, content)
                if target:
                    seen_through = result.outcome in (
                        "critical_success", "hard_success", "success",
                    )
                    verdict = "看穿" if seen_through else "试探，但未被看穿"
                    _apply_world_memory(
                        db, game_session,
                        lambda ws: world_memory.record_npc_interaction(
                            ws, target[0], dev.sequence_num,
                            f"被 {teammate.name} 用{skill}{verdict}",
                        ),
                    )
