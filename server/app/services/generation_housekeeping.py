"""长局滚动摘要与幕后世界推演。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ai import story_summarizer
from app.ai.agents import backstage_agent
from app.models.module import Module
from app.models.session import GameSession
from app.services import session_service, turn_context, world_memory
from app.services.event_protocol import make_chunk
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)
_make_chunk = make_chunk
_apply_world_memory = turn_context._apply_world_memory
_scene_name = turn_context._scene_name

# 滚动剧情摘要：最近这些事件始终保留全文、不并入摘要；「未并入摘要的事件」超过触发阈值时，
# 才把其中较老的一批与既往摘要合并浓缩一次，推进游标。控制长局上下文规模、防 KP 原地打转。
STORY_SUMMARY_KEEP_RECENT = 12
STORY_SUMMARY_TRIGGER = 24


async def _maybe_roll_story_summary(db: Session, session_id: str, llm) -> None:
    """长局滚动摘要 + 世界记忆抽取（v2）：把「未并入摘要」里较老的一批与既往摘要合并浓缩、
    推进游标，**同一次低温调用**顺带产出 NPC 记忆/线索备注的差量并合并落库。

    在 KP 每轮生成收尾（done 之后）调用；未攒够阈值时零成本返回，不额外调用 LLM。
    摘要失败：静默忽略（保持原摘要、原游标）。抽取差量失败/为空：跳过差量、摘要照常推进；
    差量落库经 ``_apply_world_memory`` fail-open，且台账 status 恒不受抽取器影响。绝不阻塞跑团。
    """
    try:
        session = db.get(GameSession, session_id)
        if not session:
            return
        events = session_service.get_session_events(db, session_id, limit=0)
        ws = session.world_state or {}
        cursor = ws.get("story_summary_seq") or 0
        # 幕后事件（仅 KP 可见）不进剧情摘要：摘要以「玩家已经历的剧情」口吻注入后续
        # 所有 KP 上下文，混入会让幕后内容脱离「幕后动态」小节的守密措辞（格式混入风险）；
        # KP 本就能从专属小节看到幕后动态，摘要里无需重复。
        uncovered = [
            e for e in events
            if (e.sequence_num or 0) > cursor
            and not session_service.is_kp_only_event(e)
        ]
        if len(uncovered) <= STORY_SUMMARY_TRIGGER:
            return
        to_summ = uncovered[: len(uncovered) - STORY_SUMMARY_KEEP_RECENT]
        if not to_summ:
            return
        # MemoryKeeper 抽取器与摘要合并为一次调用（同一批事件 + 既往摘要 + 当前 NPC 记忆摘要）
        module = db.get(Module, session.module_id) if session.module_id else None
        npc_names = {
            npc.get("id"): npc.get("name")
            for npc in ((module.npcs if module else None) or [])
            if npc.get("id")
        }
        # AI 队友清单：喂抽取器（没记忆的队友也列出，否则永远建不起第一个目标），
        # 同时作为 team_updates 落库时的白名单
        ai_teammates = session_service.get_ai_teammates(db, session_id)
        team_brief = world_memory.format_team_memory_all_brief(
            ws, {t.id: t.name for t in ai_teammates},
        )
        # 叙事主流已停但仍持锁做收尾：给前端一个可读状态，别让玩家对着无声脉冲点干等。
        room_hub.broadcast(session_id, _make_chunk("housekeeping", "KP 正在整理笔记…"))
        result = await story_summarizer.summarize_and_extract(
            llm, ws.get("story_summary") or "", to_summ,
            world_memory.format_npc_memory_all_brief(ws, npc_names),
            team_memory_brief=team_brief,
        )
        if not result:
            return
        new_summary, npc_updates, clue_notes, team_updates = result
        ws2 = dict(session.world_state or {})
        ws2["story_summary"] = new_summary
        ws2["story_summary_seq"] = to_summ[-1].sequence_num
        session.world_state = ws2
        db.commit()
        # 差量合并：只改 attitude/reason/promises/lies 与已存在线索的 note，绝不碰台账 status；
        # 队友差量另经 apply_team_memory_delta（白名单 = 本会话真实 AI 队友 id）。
        if npc_updates or clue_notes or team_updates:
            allowed = {t.id for t in ai_teammates}
            _apply_world_memory(
                db, session,
                lambda w: world_memory.apply_team_memory_delta(
                    world_memory.apply_memory_delta(w, npc_updates, clue_notes),
                    team_updates, allowed,
                ),
            )
        logger.info(
            "滚动剧情摘要更新：session=%s 游标→%s", session_id, to_summ[-1].sequence_num,
        )
    except Exception:
        logger.exception("滚动剧情摘要失败（忽略）: session=%s", session_id)


# 幕后推演触发间隔：自游标（world_state.backstage.last_run_seq）起累计的「玩家回合」
# 事件数（玩家方角色的 action/dialogue，近似计数——与导演信号的判定思路一致）。
BACKSTAGE_TURN_INTERVAL = 6
# validator 预筛：最多把最近几条幕后事件文本挂进 plan.safety.do_not_reveal
BACKSTAGE_DO_NOT_REVEAL_MAX = 3


async def _maybe_run_backstage(db: Session, session_id: str, llm) -> None:
    """幕后推演（Backstage Clock）：让世界在玩家不在场时按 NPC 的动机演进。

    触发（在 KP 每轮生成收尾处评估，不阻塞叙事主流程）：
    - 自游标起累计 ≥ ``BACKSTAGE_TURN_INTERVAL`` 个玩家回合事件；或
    - 场景已切换（``backstage.last_scene_id`` ≠ 当前场景，[SCENE_CHANGE] 的天然时间流逝点）。
    模组无带 secrets/goals 的 NPC → 永不触发、零调用；条件不满足 → 零成本返回。

    安全约束（最重要）：幕后事件绝不直接改 flags / 剧情状态——只落 event_logs
    （``visibility=["kp"]``，不广播，玩家永远不可见）+ 注入 KP 上下文；
    ``suggest_flags`` 只是给 KP 的建议，是否 [SET_FLAG] 由 KP 后续叙事决定。

    fail-open：LLM 异常 / 坏 JSON → 游标不动、无事件落库、下轮重试；任何异常不上抛。
    """
    try:
        session = db.get(GameSession, session_id)
        if not session or not session.module_id:
            return
        module = db.get(Module, session.module_id)
        if not module:
            return
        secret_npcs = backstage_agent.npcs_with_secrets(module)
        if not secret_npcs:
            return  # 模组无幕后主体：永不触发，零调用
        ws = session.world_state or {}
        bs = dict(ws.get("backstage") or {})
        if not bs:
            # 首次评估：立基线（游标 0 + 当前场景），此后场景切换才可比对触发
            _apply_world_memory(
                db, session,
                lambda w: world_memory.advance_backstage_cursor(
                    w, 0, session.current_scene_id,
                ),
            )
            bs = {"last_run_seq": 0, "last_scene_id": session.current_scene_id}
        cursor = int(bs.get("last_run_seq") or 0)
        events = session_service.get_session_events(db, session_id)
        if not events:
            return
        last_seq = int(events[-1].sequence_num or 0)
        if last_seq <= cursor:
            return
        party_ids = {session.player_character_id} | {
            c.id for c in session_service.get_party_members(db, session_id)
        }
        turns = sum(
            1 for e in events
            if (e.sequence_num or 0) > cursor
            and e.event_type in ("action", "dialogue")
            and e.actor_id in party_ids
        )
        last_scene = bs.get("last_scene_id")
        scene_changed = bool(
            last_scene and session.current_scene_id
            and session.current_scene_id != last_scene
        )
        if turns < BACKSTAGE_TURN_INTERVAL and not scene_changed:
            return

        since = [e for e in events if (e.sequence_num or 0) > cursor]
        messages = backstage_agent.build_backstage_messages(
            session, module, secret_npcs, since,
        )
        # 幕后推演刻意不广播任何 chunk（含状态提示）——幕后是「仅 KP 可见」的隔离机制，
        # 连「正在推演幕后」这类信号都不外泄（见 test_backstage 的广播禁令）。收尾期的可读
        # 状态由 _maybe_roll_story_summary 的「整理笔记」承担；此处保持静默。
        agent = backstage_agent.BackstageAgent(llm)
        valid_ids = {n.get("id") for n in (module.npcs or []) if n.get("id")}
        bevents = await agent.infer(messages, valid_ids)
        if bevents is None:
            return  # 调用/解析失败：游标不动、不落库（fail-open，下轮重试）

        npc_names = {
            n.get("id"): n.get("name") or n.get("id")
            for n in (module.npcs or []) if n.get("id")
        }
        for be in bevents:
            content = f"{npc_names.get(be['npc_id'], be['npc_id'])}：{be['action']}"
            if be.get("affected_scene"):
                content += f"（涉及：{_scene_name(module, be['affected_scene'])}）"
            # 只落库，不广播（幕后事件玩家永远不可见，无 UI）
            session_service.add_event(
                db, session_id, "system", content, actor_name="幕后",
                visibility=[session_service.KP_ONLY_SENTINEL],
                metadata={
                    "kind": "backstage",
                    "npc_id": be["npc_id"],
                    "affected_scene": be.get("affected_scene") or "",
                    "suggest_flags": be.get("suggest_flags") or [],
                },
            )
        # 推演成功（含 0 条＝「无事发生」也是结果）：游标推进到评估时的最新事件序号
        _apply_world_memory(
            db, session,
            lambda w: world_memory.advance_backstage_cursor(
                w, last_seq, session.current_scene_id,
            ),
        )
        logger.info(
            "幕后推演完成：session=%s 事件=%d 游标→%s", session_id, len(bevents), last_seq,
        )
    except Exception:
        logger.exception("幕后推演失败（忽略）: session=%s", session_id)
