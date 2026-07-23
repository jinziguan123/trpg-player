from __future__ import annotations

import asyncio
import logging
import re
import time

from sqlalchemy.orm import Session

from app.ai import turn_planner
from app.ai import tools as kp_tools
from app.ai.agents.kp_agent import KPAgent
from app.ai.context import build_kp_context
from app.ai.llm_factory import get_fast_llm, get_llm
from app.ai.prompts.kp_system import (
    CHECK_REQUEST_PROMPT,
    COMBAT_AFTERMATH_PROMPT,
    KP_DICE_CONTINUATION_PROMPT,
)
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import (
    chat_event_writer,
    command_protocol,
    dice_runtime,
    event_protocol,
    generation_lifecycle,
    generation_housekeeping,
    human_kp_actions,
    illustration_service,
    inventory_service,
    kp_actions,
    kp_tool_loop,
    narration_protocol,
    planned_effects,
    rulebook_service,
    session_service,
    team_turn_service,
    turn_context,
    turn_effects,
    turn_event_order,
)
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)

# 兼容既有调用与测试；协议的单一事实来源位于 command_protocol。
DICE_CHECK_RE = command_protocol.DICE_CHECK_RE
OPPOSED_CHECK_RE = command_protocol.OPPOSED_CHECK_RE
SAN_CHECK_RE = command_protocol.SAN_CHECK_RE
HP_CHANGE_RE = command_protocol.HP_CHANGE_RE
NPC_ACT_RE = command_protocol.NPC_ACT_RE
SCENE_CHANGE_RE = command_protocol.SCENE_CHANGE_RE
RULE_LOOKUP_RE = command_protocol.RULE_LOOKUP_RE
MODULE_LOOKUP_RE = command_protocol.MODULE_LOOKUP_RE
SET_FLAG_RE = command_protocol.SET_FLAG_RE
CLEAR_FLAG_RE = command_protocol.CLEAR_FLAG_RE
HANDOUT_RE = command_protocol.HANDOUT_RE
GROUP_RE = command_protocol.GROUP_RE
CMD_TAG_PREFIXES = command_protocol.CMD_TAG_PREFIXES
MAX_RULE_LOOKUPS = command_protocol.MAX_RULE_LOOKUPS
MAX_DICE_CONTINUATIONS = command_protocol.MAX_DICE_CONTINUATIONS
_is_cmd_tag = command_protocol.is_command_tag
_parse_tag_kv = command_protocol.parse_tag_kv
split_speech_action = event_protocol.split_speech_action
split_ooc = event_protocol.split_ooc
_make_chunk = event_protocol.make_chunk
event_to_chunk = event_protocol.event_to_chunk
_strip_speaker_prefix = narration_protocol._strip_speaker_prefix
_narr_quote_span = narration_protocol._narr_quote_span
_is_party_speaker = narration_protocol._is_party_speaker
_filter_narration_stream = narration_protocol.filter_narration_stream
_persist_error_notice = chat_event_writer.persist_error_notice
_extract_leaked_dialogue = chat_event_writer._extract_leaked_dialogue
_record_chunk_event = chat_event_writer.record_chunk_event
_resolve_scene_ref = turn_context._resolve_scene_ref
_scene_name = turn_context._scene_name
_current_turn_events = turn_context._current_turn_events
commit_pending_travel = turn_context.commit_pending_travel
_location_groups = turn_context._location_groups
_augment_plan_with_backstage = turn_context._augment_plan_with_backstage
_team_blind_message = turn_context._team_blind_message
_apply_world_memory = turn_context._apply_world_memory
_match_single_npc = turn_context._match_single_npc
_record_npc_say_memory = turn_context._record_npc_say_memory
_snap_offset = turn_context._snap_offset
_remap_marks_after_rewrite = turn_context._remap_marks_after_rewrite
_recent_seen_text = turn_context._recent_seen_text
_validate_and_patch_narration = turn_context._validate_and_patch_narration
_scene_title = turn_context._scene_title
_latest_player_input = turn_context._latest_player_input
_module_excerpts_for_context = turn_context._module_excerpts_for_context
_plan_involves_san = turn_context._plan_involves_san
_rule_keywords_from_events = turn_context._rule_keywords_from_events
_san_context = turn_context._san_context
_recent_player_text = turn_context._recent_player_text
_rule_query = turn_context._rule_query
_retrieve_rules = turn_context._retrieve_rules
_rule_excerpts_for_context = turn_context._rule_excerpts_for_context
_rule_excerpts_for_planner = turn_context._rule_excerpts_for_planner
_record_rag = turn_context._record_rag
_record_turn_usage = turn_context._record_turn_usage
DEFAULT_NPC_SKILL = dice_runtime.DEFAULT_NPC_SKILL
ALWAYS_BLIND_SKILLS = dice_runtime.ALWAYS_BLIND_SKILLS
DIFFICULTY_LABEL = dice_runtime.DIFFICULTY_LABEL
TIER_LABEL = dice_runtime.TIER_LABEL
_check_prompt_text = dice_runtime._check_prompt_text
_resolve_check_actor = dice_runtime._resolve_check_actor
_parse_bonus_penalty = dice_runtime._parse_bonus_penalty
_check_dice_detail = dice_runtime._check_dice_detail
_pool_dice_detail = dice_runtime._pool_dice_detail
_exec_generic_roll = dice_runtime._exec_generic_roll
_scene_requires_group_check = dice_runtime._scene_requires_group_check
_resolve_san_targets = dice_runtime._resolve_san_targets
_present_party = dice_runtime._present_party
_resolve_dice_group_targets = dice_runtime._resolve_dice_group_targets
_resolve_opposed = dice_runtime._resolve_opposed
_ALL_TOKENS = dice_runtime._ALL_TOKENS
# 测试与插件仍会在 chat_service 上替换校验器；保留模块级兼容引用。
turn_validator = turn_context.turn_validator
MAX_TEAMMATES_PER_TURN = team_turn_service.MAX_TEAMMATES_PER_TURN
TEAM_ACTION_EVENT = team_turn_service.TEAM_ACTION_EVENT
_matcher_npcs = team_turn_service._matcher_npcs
_stream_narration_filtered = team_turn_service._stream_narration_filtered
_parse_team_decision = team_turn_service._parse_team_decision
_run_team_turn = team_turn_service._run_team_turn
TeamAgent = team_turn_service.TeamAgent
_classify_llm_error = generation_lifecycle.classify_llm_error
_housekeeping_manager = generation_lifecycle.HousekeepingManager()
_housekeeping_tasks = _housekeeping_manager.tasks
STORY_SUMMARY_KEEP_RECENT = generation_housekeeping.STORY_SUMMARY_KEEP_RECENT
STORY_SUMMARY_TRIGGER = generation_housekeeping.STORY_SUMMARY_TRIGGER
BACKSTAGE_TURN_INTERVAL = generation_housekeeping.BACKSTAGE_TURN_INTERVAL
BACKSTAGE_DO_NOT_REVEAL_MAX = generation_housekeeping.BACKSTAGE_DO_NOT_REVEAL_MAX
_maybe_roll_story_summary = generation_housekeeping._maybe_roll_story_summary
_maybe_run_backstage = generation_housekeeping._maybe_run_backstage
story_summarizer = generation_housekeeping.story_summarizer
backstage_agent = generation_housekeeping.backstage_agent
_illustrate_event = illustration_service._illustrate_event
_illustrate_handout = illustration_service._illustrate_handout
_spawn_illustration = illustration_service._spawn_illustration
_module_list_cache_writer = illustration_service._module_list_cache_writer
_scene_variant_cache_writer = illustration_service._scene_variant_cache_writer
_module_era = illustration_service._module_era
_scene_visual_state = illustration_service._scene_visual_state
_scene_card_key = illustration_service._scene_card_key
_maybe_scene_illustration = illustration_service._maybe_scene_illustration
_maybe_clue_illustration = illustration_service._maybe_clue_illustration
_maybe_encounter_illustration = illustration_service._maybe_encounter_illustration
_attach_npc_portrait = illustration_service._attach_npc_portrait
_attach_npc_portraits = illustration_service._attach_npc_portraits
_PORTRAIT_INFLIGHT = illustration_service._PORTRAIT_INFLIGHT


async def _drain_housekeeping(session_id: str) -> None:
    """等待上一轮后台收尾，避免 world_state 并发读改写。"""
    await _housekeeping_manager.drain(session_id)


def _spawn_housekeeping(session_id: str, llm) -> None:
    """启动独立数据库会话中的摘要与幕后推演。"""
    _housekeeping_manager.spawn(
        session_id,
        llm,
        _maybe_roll_story_summary,
        _maybe_run_backstage,
    )


async def _finish_generation(db: Session, session_id: str, llm) -> None:
    """先广播完成，再异步启动收尾。"""
    room_hub.broadcast(session_id, _make_chunk("done"))
    _spawn_housekeeping(session_id, llm)


def _persist_narration(
    db: Session, session_id: str, result: list, event_order: list | None = None,
) -> None:
    """兼容入口；叙事清洗和落库由 chat_event_writer 负责。"""
    chat_event_writer.persist_narration(
        db,
        session_id,
        result,
        event_order,
        attach_npc_portraits=_attach_npc_portraits,
    )


def _record_clue_ledger_from_plan(
    db: Session,
    game_session: GameSession,
    plan: turn_planner.TurnPlan,
    events: list,
    player_char: Character,
    teammates: list[Character] | None,
    module: Module | None = None,
) -> None:
    """兼容入口；世界记忆更新由 turn_context 负责，首次线索配图通过端口触发。"""
    turn_context.record_clue_ledger_from_plan(
        db,
        game_session,
        plan,
        events,
        player_char,
        teammates,
        module,
        on_first_clue=_maybe_clue_illustration,
    )

# DICE_CHECK 升级为键值解析（参数顺序无关）：skill=必填；difficulty/char/chars/visibility 选填。
# char=对谁投（空/主角=主角，队友名，NPC 名）；visibility=open|blind（blind=暗投/暗骰，结果只给 KP）。
# KP 有时（尤其多人回合）不发 [DICE_CHECK]、而是把「X 检定（normal）：困难成功 (10 ≤ 60)」这类
# **机检结果行**当散文写进旁白——那本是系统掷骰后才产生的内容，KP 自撰＝伪造结果，且玩家看不到
# 投骰提示/动画、结果卡也渲染不出。落库前确定性剥除这类行（要求「检定（<真实难度词>）：<成败等级>」
# 连写，机检签名极强、正常叙事不会出现，误伤概率极低）。配套 kp_system 规则3 的提示词硬约束。
# 对抗骰：两方各投同名或不同技能，比成功等级。a/b 为角色名（主角/队友/NPC）。
# SAN_CHECK 升级为键值解析：success_loss/failure_loss + chars=（目睹者，缺省在场全体）
# + source=（恐怖源标识，用于「同一角色对同一恐怖只检定一次」的去重）。各角色各自结算。
# 模组原文查阅：与 RULE_LOOKUP 同一套终止性指令模式，共享每轮查阅配额。
# 剧情状态推进：KP 在叙事节拍发 [SET_FLAG: flag=xxx] 置标志、[CLEAR_FLAG: flag=xxx] 清标志，
# 场景/NPC 的状态变体据此切换（如「地下室进水后变致命」「危险消退」）。是内部控制标签，不展示给玩家。
# 容忍：漏写「flag=」、冒号写成空格（如「[SET_FLAG hint_x]」）。全角括号在处理前已归一为半角。
# 手书发放：KP 在剧情达成发放条件时发 [HANDOUT: id=xxx]，系统把该手书原文以信笺卡片发给全桌。
# 容忍漏写「id=」、冒号写成空格（与 SET_FLAG 同款宽容）。全角括号在处理前已归一为半角。
# 分头行动：KP 在每个分组/场景内容前标 [GROUP: scene=<场景标签>]，后续内容归该组，前端据此分栏。内联剔除。
# 注：场景瓦片地图已下线，[MOVE]/[MAP_MARK] 不再广告也不再执行；流过滤器仍静默吞掉这两个
# 标签的残余文本形态（见 _stream_narration_filtered 的 startswith 分支），防止泄给玩家。















def _reorder_turn_events(
    db: Session, session_id: str, event_order: list, base_seq: int
) -> None:
    """兼容旧调用点；实际实现位于独立的回合事件顺序服务。"""
    turn_event_order.reorder_turn_events(db, session_id, event_order, base_seq)




SPLIT_FOCUS_PROMPT = (
    "本回合队伍分头行动。现在【只】叙述「{label}」这个场景里发生的事：描写此地的环境、气氛，"
    "以及在场 NPC 对 {members} 言行的反应与由此推进的后续。\n"
    "要求：①详尽完整，与其他分组同等篇幅；②只写这一场景，绝不叙述或提及其他分组的人"
    "（他们另行单独叙述）；③{members} 都是**玩家角色**——**绝不替他们说话、行动、做决定或描写其"
    "心理感受**，只呈现世界与 NPC 对其已有言行的回应；"
    "④**此地的 NPC 对其他分组在别处的言行一无所知**（除非大到隔墙可闻的巨响、或有人当面告知）——"
    "绝不让 NPC 评论、追问或以任何方式反应它感知之外的事。"
)








# 兼容既有调用；规划副作用的单一实现位于 planned_effects。
_ensure_planned_combat = planned_effects._ensure_planned_combat
_san_rolled_this_turn = planned_effects._san_rolled_this_turn
_ensure_planned_sanity = planned_effects._ensure_planned_sanity
_hp_changed_this_turn = planned_effects._hp_changed_this_turn
_ensure_planned_mishap = planned_effects._ensure_planned_mishap
_ensure_planned_items = planned_effects._ensure_planned_items
_ensure_planned_combat_damage = planned_effects._ensure_planned_combat_damage
_ensure_planned_scene = planned_effects._ensure_planned_scene




async def _run_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None = None,
    blind_results: list[str] | None = None,
    plan: turn_planner.TurnPlan | None = None,
) -> None:
    llm = get_llm()
    kp = KPAgent(llm)
    # 仅在非开场、且该规则系统已挂载规则书时，向 KP 广告 [RULE_LOOKUP] 能力
    rules_enabled = bool(events) and rulebook_service.has_rulebook(db, module.rule_system)
    # 仅在非开场、且模组原文索引就绪时，向 KP 广告 [MODULE_LOOKUP] 能力（镜像规则书模式）
    module_rag_enabled = bool(events) and getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (teammates or [])}
    matcher_npcs = _matcher_npcs(module, teammates, game_session)
    # 生成前基线序号：供确定性 SAN 守卫判断「本轮 KP 是否已自行掷过 SAN」（幂等，防重复扣）。
    pre_gen_seq = session_service.get_next_sequence_num(db, session_id) - 1

    # 回合裁定计划：主链路（run_chat_generation）已在队友回合之前先跑好 plan 并记过线索台账，
    # 通过 plan 参数传入 → 此处不重复调用。其他入口（run_travel_generation / _run_kp_turn 尾部）
    # 不传 plan → 这里现跑并记账（钩子 a），行为与前移前完全一致。开场（无事件）不跑。
    if plan is None and events:
        plan_messages = turn_planner.build_turn_plan_messages(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled,
            rule_excerpts=_rule_excerpts_for_planner(db, module, events, game_session),
        )
        plan = await turn_planner.run_turn_planner(get_fast_llm(), plan_messages)
        # 世界记忆钩子 a：本轮裁定要揭示线索 → 写入线索台账（纯确定性，零额外 LLM 调用）
        if plan is not None:
            _record_clue_ledger_from_plan(
                db, game_session, plan, events, player_char, teammates, module=module,
            )

    # 幕后推演 → validator 预筛：最近幕后事件文本挂进 plan.safety.do_not_reveal，
    # 防 KP 把「玩家不可见」的幕后动态直接复述进旁白（单场景与分头路径共用此 plan）。
    if plan is not None:
        _augment_plan_with_backstage(plan, events)

    # 分头行动：按各成员「真实所在场景」归并（玩家经大地图、队友经 travel 动作更新的确定性位置）。
    # 身处 ≥2 个场景即分头 → 逐场景生成叙事。不再靠 LLM 猜分组、也不因「打算去X」误判。
    scene_groups = _location_groups(game_session, module, player_char, teammates)

    # 本回合队友暗骰（心理学等）的真实结果 → 一条「仅 KP 可见」的上下文消息（不落库/不广播）。
    blind_message = _team_blind_message(blind_results)

    # 规则书要点被动注入：按本轮 plan.turn_kind 预取规则条文（与 [RULE_LOOKUP] 主动查互补）。
    # 规则片段不依赖场景，分头行动时各分组共用同一份检索结果（与模组摘录「分头也注入」对齐）。
    rule_excerpts = _rule_excerpts_for_context(db, module, plan, events, game_session)

    if len(scene_groups) >= 2:
        # 分头行动 v1 仍走旧正则路径（与 use_tool_calls 开关无关）：多分组编排与
        # loop 的 group 标签/指令归并交互留待下一步，先保证主路径可开关灰度。
        await _run_split_generation(
            db, session_id, game_session, module, player_char, events,
            teammates, kp, llm, rules_enabled, matcher_npcs, scene_groups,
            plan=plan, blind_message=blind_message, rule_excerpts=rule_excerpts,
        )
        return

    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=rules_enabled,
        module_excerpts=_module_excerpts_for_context(
            db, module, game_session, events, party_ids,
        ),
        module_lookup_enabled=module_rag_enabled,
        rule_excerpts=rule_excerpts,
    )
    # 战斗结果摘要已注入本轮上下文 → 清除，避免下一轮重复注入（读一次）。
    if (game_session.world_state or {}).get("combat_result"):
        ws = dict(game_session.world_state)
        ws.pop("combat_result", None)
        game_session.world_state = ws
        db.commit()
    if plan is not None:
        messages.append(turn_planner.build_turn_plan_message(plan))
    if blind_message is not None:
        messages.append(blind_message)

    # 玩家党名单（玩家 + AI 队友）：供台词归属守卫用——KP 绝不能用气泡替他们说话。
    party_names = {player_char.name} | {t.name for t in (teammates or [])}
    result = ["", "", [], [], []]
    if _tool_loop_active(llm):
        # 新路径：agent loop（标准工具调用）。指令在 loop 内经执行器完成（含文本指令兜底），
        # 不再走 _process_commands；validator 终检/落库/记忆钩子与旧路径共用（见下方）。
        messages.append(kp_tools.tool_mode_message())
        exclude = set()
        if not rules_enabled:
            exclude.add("rule_lookup")   # 未挂规则书：不提供该工具（镜像旧路径不广告）
        if not module_rag_enabled:
            exclude.add("module_lookup")  # 原文索引未就绪：同上
        execute = _build_kp_tool_executor(
            db, session_id, game_session, module, player_char, teammates, llm, result,
        )
        # 本轮基线序号 + 事件广播顺序清单：loop 内工具事件即时落库（较小序号），旁白收尾才落库，
        # 直接 resync 会顺序错乱；收尾按广播偏移把本轮（seq>base_seq）事件重排回交错顺序。
        base_seq = session_service.get_next_sequence_num(db, session_id) - 1
        event_order: list = []
        try:
            async for chunk in _run_kp_agent_loop(
                llm, messages, result, execute,
                tools=kp_tools.openai_tool_schemas(exclude=exclude),
                npcs=matcher_npcs, plan=plan, party_names=party_names,
                event_order=event_order,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            _persist_narration(db, session_id, result, event_order)
            _reorder_turn_events(db, session_id, event_order, base_seq)
            raise
        _record_turn_usage(db, game_session, llm, events)   # validator 前，趁 last_usage 仍是主叙事那次
        await _validate_and_patch_narration(
            llm, plan, result, event_order, seen_context=_recent_seen_text(events))
        _persist_narration(db, session_id, result, event_order)
        _reorder_turn_events(db, session_id, event_order, base_seq)
        # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
        )
    else:
        # 旧路径：单次流式生成 + 正则指令后处理（降级开关，行为不变）。
        # 取消（硬取消 task）或流式中途报错（如供应商抖动断流）时，已生成的叙事都要落库，
        # 否则客户端在收到 done 后 resync 会拉到空历史，造成「生成到一半聊天全部消失」。
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, result, npcs=matcher_npcs, party_names=party_names,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            # CancelledError(继承 BaseException) 与普通异常都先把已生成片段落库再上抛
            _persist_narration(db, session_id, result)
            raise
        _record_turn_usage(db, game_session, llm, events)   # validator 前，趁 last_usage 仍是主叙事那次
        await _validate_and_patch_narration(
            llm, plan, result, seen_context=_recent_seen_text(events))
        _persist_narration(db, session_id, result)
        # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
        )

        async for chunk in _process_commands(
            db, session_id, result[1], module, player_char, game_session, llm,
            teammates=teammates,
        ):
            room_hub.broadcast(session_id, chunk)

    async for chunk in _ensure_planned_combat(
        db, session_id, game_session, module, player_char, teammates, llm, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性 SAN 守卫：计划裁定本轮目睹恐怖但 KP 漏发 SAN → 后端补发（幂等）。
    async for chunk in _ensure_planned_sanity(
        db, session_id, game_session, player_char, teammates, plan, pre_gen_seq,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性库存守卫：计划裁定的物品获得/失去 → 后端确定性增减（幂等），库存是权威状态。
    async for chunk in _ensure_planned_items(
        db, session_id, game_session, player_char, teammates, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性战斗伤害守卫：战斗中非常规/范围攻击 → 挂成玩家 pending_roll 亲手掷、扣敌人 HP。
    async for chunk in _ensure_planned_combat_damage(db, session_id, player_char, plan):
        room_hub.broadcast(session_id, chunk)

    # 确定性场景守卫：计划裁定玩家本轮真实移动 → 后端把角色位置/大地图切过去（幂等），补 KP 漏切。
    async for chunk in _ensure_planned_scene(
        db, session_id, game_session, module, player_char, teammates, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    await _finish_generation(db, session_id, llm)


def _tag_turn_events_by_group(db: Session, turn_events: list, groups: list[dict]) -> None:
    """把本回合各角色的事件按其所在分组补打 group 标签（玩家行动随其场景列同列）。

    掷骰事件（actor=系统）按其内容里的领头角色名（「亨利·卡特｜…」）归组。
    """
    name_to_group: dict[str, str] = {}
    for g in groups:
        for m in g["members"]:
            name_to_group[m] = g["label"]

    def _match(name: str) -> str | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in name_to_group:
            return name_to_group[name]
        for full, label in name_to_group.items():
            if name in full or full in name:
                return label
        return None

    for e in turn_events:
        etype = getattr(e, "event_type", None)
        if etype not in ("action", "dialogue", "dice"):
            continue
        label = _match(e.actor_name or "")
        if not label and etype == "dice":
            head = re.split(r"[｜|]", e.content or "", 1)[0]
            label = _match(head)
        if label:
            session_service.set_event_group(db, e, label)


async def _run_split_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None,
    kp: KPAgent,
    llm,
    rules_enabled: bool,
    matcher_npcs: list[dict],
    groups: list[dict],
    plan: turn_planner.TurnPlan | None = None,
    blind_message: dict | None = None,
    rule_excerpts: list[dict] | None = None,
) -> None:
    """分头行动：对每个分组各跑一次聚焦叙事，后端确定性地把产物归入该组。

    每组单独生成 → 篇幅均衡、不会「只详写最后一个场景」；分组标签由后端注入 →
    前端实时/重连都能稳定分栏，不靠模型自觉打 [GROUP]。
    命令（检定/HP/旗标/场景）在所有分组叙事完成后，对合并文本统一处理一次。
    ``plan`` 是本回合唯一的裁定计划（跨分组共用），每组都注入一份、也各自校验一次——
    分头场景 NPC/线索并行推进，同样需要 clue_policy/safety 兜底，不能因为分头
    就退化回纯提示词。
    """
    # 先把本回合各角色的行动/对话/掷骰也归入其所在场景列：这样每一列＝该场景里
    # 「玩家行动 + KP 叙事」自成一体（而非行动全挤在主线、叙事另起一列）。
    # 位置已由显式移动（玩家大地图 / 队友 travel 动作）确定性写入，此处不再据分组反推搬人。
    _tag_turn_events_by_group(db, _current_turn_events(events), groups)
    # 生成前基线序号：供确定性 SAN 守卫判断本轮 KP 是否已自行掷过 SAN（幂等）。
    pre_gen_seq = session_service.get_next_sequence_num(db, session_id) - 1
    plan_message = turn_planner.build_turn_plan_message(plan) if plan is not None else None

    # 模组原文 RAG：与单场景路径同一门槛（索引就绪才广告 [MODULE_LOOKUP]/注入摘录）
    module_rag_enabled = bool(events) and getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (teammates or [])}

    combined: list[str] = []
    for grp in groups:
        label = grp["label"]
        members = "、".join(grp["members"])
        # 关键：以该组所在场景为锚构建上下文，否则每列都拿主角场景的 NPC/线索，
        # KP 只能把主角场景重复叙述一遍（两列讲同一件事）。
        messages = build_kp_context(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled, viewer_scene_id=grp.get("scene_id"),
            module_excerpts=_module_excerpts_for_context(
                db, module, game_session, events, party_ids,
                scene_id=grp.get("scene_id"),
            ),
            module_lookup_enabled=module_rag_enabled,
            # 规则要点不依赖场景：各分组共用调用方预取的同一份（与模组摘录注入现状对齐）
            rule_excerpts=rule_excerpts,
        )
        if plan_message is not None:
            messages.append(plan_message)
        if blind_message is not None:
            messages.append(blind_message)
        messages.append({
            "role": "user",
            "content": SPLIT_FOCUS_PROMPT.format(label=label, members=members),
        })
        result = ["", "", [], [], []]
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, result, npcs=matcher_npcs, group_label=label,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            _persist_narration(db, session_id, result)
            raise
        await _validate_and_patch_narration(
            llm, plan, result, seen_context=_recent_seen_text(events))
        _persist_narration(db, session_id, result)
        # 世界记忆钩子 c：本组 NPC 台词记入其互动史（听众＝该组成员，信息不跨组共享）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2], grp["members"],
        )
        combined.append(result[1])

    async for chunk in _process_commands(
        db, session_id, "\n".join(combined), module, player_char, game_session, llm,
        teammates=teammates,
    ):
        room_hub.broadcast(session_id, chunk)

    async for chunk in _ensure_planned_combat(
        db, session_id, game_session, module, player_char, teammates, llm, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性 SAN 守卫：计划裁定本轮目睹恐怖但 KP 漏发 SAN → 后端补发（幂等）。
    async for chunk in _ensure_planned_sanity(
        db, session_id, game_session, player_char, teammates, plan, pre_gen_seq,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性库存守卫：计划裁定的物品获得/失去 → 后端确定性增减（幂等），库存是权威状态。
    async for chunk in _ensure_planned_items(
        db, session_id, game_session, player_char, teammates, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性战斗伤害守卫：战斗中非常规/范围攻击 → 挂成玩家 pending_roll 亲手掷、扣敌人 HP。
    async for chunk in _ensure_planned_combat_damage(db, session_id, player_char, plan):
        room_hub.broadcast(session_id, chunk)

    # 确定性场景守卫：计划裁定玩家本轮真实移动 → 后端把角色位置/大地图切过去（幂等），补 KP 漏切。
    async for chunk in _ensure_planned_scene(
        db, session_id, game_session, module, player_char, teammates, plan,
    ):
        room_hub.broadcast(session_id, chunk)

    await _finish_generation(db, session_id, llm)


def _skill_names(char: Character) -> list[str]:
    """从角色身上尽可能取出技能名（skills / system_data.skills，兼容 dict 或 list 形态）。"""
    names: set[str] = set()

    def _harvest(obj):
        if isinstance(obj, dict):
            names.update(str(k) for k in obj.keys())
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, dict) and it.get("name"):
                    names.add(str(it["name"]))

    _harvest(getattr(char, "skills", None))
    sd = getattr(char, "system_data", None)
    if isinstance(sd, dict):
        _harvest(sd.get("skills"))
    return sorted(names)


_COMBAT_DECLARATION_RE = re.compile(
    r"攻击|袭击|开枪|射击|开火|砍向|劈向|刺向|捅向|挥(?:刀|剑|斧)|"
    r"(?:冲|扑)上去.{0,16}(?:打|揍|攻击|砍|劈|刺)|(?:一拳|一脚|踢向|拳打)"
)


def _looks_like_combat_declaration(text: str) -> bool:
    """高精度识别明确交战宣言，只用于避免被普通检定分诊提前截走。"""
    if re.search(r"(?:不要|别|停止|阻止).{0,6}(?:攻击|袭击|开枪|射击|开火)", text or ""):
        return False
    return bool(_COMBAT_DECLARATION_RE.search(text or ""))


def _team_guidance_from_plan(plan: turn_planner.TurnPlan | None) -> str:
    """从 plan.direction 派生给 AI 队友的软指引（目前只用 spotlight——把戏份让给冷场玩家）。

    只影响队友「优先照顾谁」，不授权队友替人决定/代言；无 plan 或无 spotlight 则为空串。
    """
    if plan is None or not plan.direction.spotlight:
        return ""
    return (
        "本轮请把互动机会和话头多留给："
        + "、".join(plan.direction.spotlight)
        + "（他们最近戏份偏少）。你仍然只能决定自己的言行，不得替他们做决定或代言。"
    )


async def run_chat_generation(session_id: str) -> None:
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        ai_teammates = session_service.get_ai_teammates(db, session_id)
        # KP 上下文整队：主角之外的所有已填角色（真人 + AI），让 KP 知道全员在场
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        get_llm()   # fail-fast：未配置 AI 时在此就近报「请到设置页配置」，不深入半截流程

        # 一阵疯狂计时：本回合开始给在场角色的临时疯狂发作各减 1 回合，到期自动解除并广播恢复。
        for chunk in _tick_madness_recovery(db, session_id, [player_char, *party_others]):
            room_hub.broadcast(session_id, chunk)

        # 取本轮玩家文本（意图分诊已并入 planner 的 player_check_request 字段，
        # 不再单独跑一次分诊 LLM 调用——省一段串行延迟）。
        turn = _current_turn_events(session_service.get_session_events(db, session_id))
        # 本轮暂存的「前往」动作（大地图前往加入本回合）已随推进转正 → 在建 KP 上下文前
        # 确定性同步该角色所在场景，随后 KP 会以正确位置叙述抵达见闻（无需再单独走一次生成）。
        commit_pending_travel(db, session_id, turn)
        actor_id = next(
            (e.actor_id for e in turn if e.event_type in ("action", "dialogue") and e.actor_id), None,
        )
        acting = (db.get(Character, actor_id) if actor_id else None) or player_char
        player_text = " ".join(
            (e.content or "") for e in turn
            if e.event_type in ("action", "dialogue") and e.actor_id == acting.id and (e.content or "").strip()
        )

        # planner 前移：在队友回合之前先跑一次裁定计划，作为本回合的共享契约——队友据
        # plan.direction 派生的导演提示行动（如把话头递给冷场玩家），KP 叙事时再以队友实际
        # 行动 + plan 为准。plan 是「裁定意图」不是「剧本」，队友行动后语义不变；开场不跑。
        # 结构化副任务走快模型（get_fast_llm，未配置时即主模型）。
        pre_events = session_service.get_session_events(db, session_id)
        plan = None
        fast_llm = get_fast_llm()
        if pre_events:
            room_hub.broadcast(session_id, _make_chunk("housekeeping", "守秘人正在判读局势…"))
            t_plan = time.monotonic()
            rules_enabled = rulebook_service.has_rulebook(db, module.rule_system)
            plan_messages = turn_planner.build_turn_plan_messages(
                game_session, module, player_char, pre_events,
                teammates=party_others, rules_lookup_enabled=rules_enabled,
                rule_excerpts=_rule_excerpts_for_planner(db, module, pre_events, game_session),
            )
            plan = await turn_planner.run_turn_planner(fast_llm, plan_messages)
            logger.info(
                "耗时|planner %.1fs session=%s", time.monotonic() - t_plan, session_id,
            )
            # 世界记忆钩子 a：本轮裁定要揭示线索 → 写入线索台账（前移后在此统一记账）
            if plan is not None:
                _record_clue_ledger_from_plan(
                    db, game_session, plan, pre_events, player_char, party_others,
                    module=module,
                )

        # 玩家明确申请检定（plan.player_check_request）→ 直接走确定性检定裁定
        # （避免被 KP 当叙事顺过去），不再跑队友回合与常规叙事。战斗宣言不走此路。
        requested_skill = (plan.player_check_request if plan else "").strip()
        if (
            requested_skill and player_text
            and not _looks_like_combat_declaration(player_text)
            and not (plan and plan.combat.should_start)
        ):
            await _run_kp_turn(
                db, session_id, game_session, module, player_char, party_others,
                CHECK_REQUEST_PROMPT.format(
                    actor=acting.name, skill=requested_skill, intent=player_text,
                ),
            )
            return

        # 玩家输入后：先跑一轮 AI 队友自动响应（仅 AI 席、仅一轮、不自触发），再交 KP 收束。
        # 队友暗骰（心理学等）的真实结果收集到 team_blind，注入本回合 KP 上下文而不落库/广播。
        team_blind: list[str] = []
        if ai_teammates:
            t_team = time.monotonic()
            async for chunk in _run_team_turn(
                db, session_id, game_session, module, player_char, ai_teammates, fast_llm,
                blind_results=team_blind,
                team_guidance=_team_guidance_from_plan(plan),
            ):
                room_hub.broadcast(session_id, chunk)
            logger.info(
                "耗时|队友回合 %.1fs（%d 人）session=%s",
                time.monotonic() - t_team, len(ai_teammates), session_id,
            )

        events = session_service.get_session_events(db, session_id)
        t_kp = time.monotonic()
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others, blind_results=team_blind, plan=plan,
        )
        logger.info(
            "耗时|KP 叙事 %.1fs session=%s", time.monotonic() - t_kp, session_id,
        )
    except asyncio.CancelledError:
        logger.info("生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("生成失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（KP 生成中断，请重试或继续输入）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def _run_kp_turn(
    db, session_id, game_session, module, player_char, party_others, user_prompt: str,
    then_team_turn: list[Character] | None = None,
    sanity_guard: bool = False,
    mishap_guard: bool = False,
) -> None:
    """跑一轮 KP：注入 user_prompt → 流式叙事 → 处理指令（待定检定/掷骰/场景等）→ done。

    ``then_team_turn`` 给定时（如玩家大地图前往后），在 KP 叙事与指令处理之后、``done`` 之前
    再跑一轮 AI 队友回合——否则这条路（不经 run_chat_generation）的队友永远没有发言机会。

    ``sanity_guard`` 给定时（检定后续写等路径）：本函数默认不跑 planner/SAN 守卫，但检定成功
    揭示的恐怖是在**叙事生成时**才出现的（回合起点的 plan 看不到），故在叙事之后现跑一次 planner
    （此时上下文已含刚揭示的恐怖）→ 确定性补发 SAN；KP 已自发掷过 SAN 则幂等跳过、不重复扣。
    """
    llm = get_llm()
    # SAN 守卫基线：本次续写生成前的最大 seq，用于判断 KP 是否已自行掷过 SAN（幂等）。
    pre_gen_seq = session_service.get_next_sequence_num(db, session_id) - 1
    events = session_service.get_session_events(db, session_id)
    rules_enabled = rulebook_service.has_rulebook(db, module.rule_system)
    module_rag_enabled = getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (party_others or [])}
    messages = build_kp_context(
        game_session, module, player_char, events,
        teammates=party_others, rules_lookup_enabled=rules_enabled,
        module_excerpts=_module_excerpts_for_context(
            db, module, game_session, events, party_ids,
        ),
        module_lookup_enabled=module_rag_enabled,
    )
    messages.append({"role": "user", "content": user_prompt})

    kp = KPAgent(llm)
    res = ["", "", [], [], []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, res, npcs=_matcher_npcs(module, party_others, game_session),
        ):
            room_hub.broadcast(session_id, chunk)
    except asyncio.CancelledError:
        _persist_narration(db, session_id, res)
        raise
    _persist_narration(db, session_id, res)
    # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
    _record_npc_say_memory(
        db, session_id, game_session, module, res[2],
        [player_char.name] + [t.name for t in (party_others or [])],
    )

    async for chunk in _process_commands(
        db, session_id, res[1], module, player_char, game_session, llm,
        teammates=party_others,
    ):
        room_hub.broadcast(session_id, chunk)

    # 确定性后果守卫（检定后续写等路径）：恐怖揭示 / 大失败身体反噬都是在**叙事生成时**才定的
    # （回合起点的 plan 看不到），故在叙事之后现跑一次 planner——此时上下文已含刚揭示的恐怖与
    # 大失败结果——据其 sanity / mishap 裁定确定性补发 SAN / 扣血。KP 已自发掷 SAN / 扣血则各自幂等跳过。
    need_sanity = sanity_guard and not _san_rolled_this_turn(db, session_id, pre_gen_seq)
    need_mishap = mishap_guard and not _hp_changed_this_turn(db, session_id, pre_gen_seq)
    if need_sanity or need_mishap:
        post_events = session_service.get_session_events(db, session_id)
        rules_enabled = bool(post_events) and rulebook_service.has_rulebook(db, module.rule_system)
        plan_messages = turn_planner.build_turn_plan_messages(
            game_session, module, player_char, post_events, teammates=party_others,
            rules_lookup_enabled=rules_enabled,
            rule_excerpts=_rule_excerpts_for_planner(db, module, post_events, game_session),
        )
        plan = await turn_planner.run_turn_planner(get_fast_llm(), plan_messages)
        if need_sanity:
            async for chunk in _ensure_planned_sanity(
                db, session_id, game_session, player_char, party_others, plan, pre_gen_seq,
            ):
                room_hub.broadcast(session_id, chunk)
        if need_mishap:
            async for chunk in _ensure_planned_mishap(
                db, session_id, player_char, party_others, plan, pre_gen_seq, module=module,
            ):
                room_hub.broadcast(session_id, chunk)

    if then_team_turn:
        db.refresh(game_session)  # 叙事里可能有 [SCENE_CHANGE]/[MOVE] 改了位置，重取再判分头
        async for chunk in _run_team_turn(
            db, session_id, game_session, module, player_char, then_team_turn, get_fast_llm(),
        ):
            room_hub.broadcast(session_id, chunk)

    await _finish_generation(db, session_id, llm)


async def run_check_request_generation(
    session_id: str, actor_id: str, skill: str, intent: str = "",
) -> None:
    """玩家『申请』检定：交 KP 裁定本次是否需要检定、用什么难度（玩家不指定难度）。

    ``intent`` 是玩家顺带说明的检定目标（如「查书桌暗格」）——现场同时有多条线索/多个
    可疑点时，光报技能名 KP 猜不出具体针对什么，必须带上这句话才能裁定到位。
    KP 若判定需要，会输出 [DICE_CHECK]，经 _process_commands 挂成「待玩家投骰」；
    若判定无需检定，则直接简短叙述。"""
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        actor = db.get(Character, actor_id) or player_char
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            CHECK_REQUEST_PROMPT.format(
                actor=actor.name, skill=skill,
                intent=intent.strip() or "（未说明，需你结合当前情境自行判断意图）",
            ),
        )
    except asyncio.CancelledError:
        logger.info("检定申请生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("检定申请生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "生成出错，请重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_combat_aftermath_generation(session_id: str) -> None:
    """战斗/追逐结束后**主动**生成余波叙述——无需玩家先开口。

    复用既有「combat_result 折回主 KP」通道：build_kp_context 会把结果摘要注入本轮上下文，
    KP 承接直接后果、交代在场者状态、把主动权交还调查员。读一次即清 combat_result，
    避免玩家下一次行动时 _run_generation 再注入一遍余波。无结果摘要 / 无 LLM 则安静收场。
    """
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        if not game_session or not (game_session.world_state or {}).get("combat_result"):
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            COMBAT_AFTERMATH_PROMPT,
        )
        # 读一次即清：_run_kp_turn 走 build_kp_context 注入了 combat_result 但不清除
        # （只有 _run_generation 会清），这里补清，避免下一次玩家回合重复注入余波。
        db.refresh(game_session)
        if (game_session.world_state or {}).get("combat_result"):
            ws = dict(game_session.world_state)
            ws.pop("combat_result", None)
            game_session.world_state = ws
            db.commit()
    except asyncio.CancelledError:
        logger.info("战斗余波生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("战斗余波生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_roll_generation(session_id: str, check_id: str) -> None:
    """玩家点『投骰』：取出待定检定 → 按 KP 定的难度掷骰 → 广播达成等级 → KP 据等级续写。"""
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        check = session_service.pop_pending_check(db, session_id, check_id)
        if not check:
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )

        skill = check["skill"]
        difficulty = check.get("difficulty", "normal")
        source = check.get("source", "")
        bonus = int(check.get("bonus") or 0)
        penalty = int(check.get("penalty") or 0)
        char_data, disp_name, _is_npc, _cid = _resolve_check_actor(
            check.get("char_ref", ""), skill, player_char, party_others, module,
        )
        # 临时疯狂：症状波及该技能域 → 自动加惩罚骰（确定性，不问 KP 当初有没有标 penalty）。
        from app.rules.coc import madness as coc_madness
        penalty += coc_madness.check_penalty((char_data.get("system_data") or {}).get("madness"), skill)
        engine = get_engine(module.rule_system)
        result = engine.resolve_check(char_data, skill, difficulty, bonus=bonus, penalty=penalty)
        tier_cn = TIER_LABEL.get(result.tier, result.tier)

        dice_content = (
            f"{disp_name}｜{skill} 检定（{difficulty}）：{tier_cn}（{result.description}）"
        )
        dice_meta = {
            "skill": skill, "skill_value": result.skill_value, "roll": result.roll,
            "target": result.target, "outcome": result.outcome, "tier": result.tier,
            "actor": disp_name, "dice": _check_dice_detail(result),
        }
        ev = session_service.add_event(
            db, session_id, "dice", dice_content, actor_name="系统", metadata=dice_meta,
        )
        room_hub.broadcast(
            session_id,
            _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id),
        )

        # 治疗类检定成功 → 引擎确定性回血（不靠 KP 自觉发 HP_CHANGE）。广播结算，并把结果并进
        # 回灌 KP 的描述，让 KP 据「已回 N 点」续写而非自己臆断/漏结算。
        heal_note = ""
        heal_target_id = check.get("heal_target_id")
        if heal_target_id:
            target_char = db.get(Character, heal_target_id)
            for chunk in _apply_heal_on_success(db, session_id, target_char, skill, result.outcome):
                room_hub.broadcast(session_id, chunk)
                heal_note = "；系统已按规则确定性结算回血"

        desc = (
            f"{disp_name} {skill}（{difficulty}），达成 {tier_cn}"
            + (f"（针对：{source}）" if source else "")
            + f"：{result.description}{heal_note}"
        )
        if game_session.kp_mode == "human":
            # 真人 KP 模式下掷骰只完成确定性结算，不自动生成后续叙事；KP 可据结果手动发布。
            room_hub.broadcast(
                session_id,
                _make_chunk("kp_roll_ready", "检定已结算，等待真人 KP 处理后果", metadata={"description": desc}),
            )
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        # 恐怖多在**检定成功**时才被揭示（看清那具尸体…）；仅成功时才在叙事后补跑 planner
        # 判理智（失败不多花这次调用）。失败若也揭示了恐怖，仍可由 KP 自发 [SAN_CHECK] 兜底。
        # 大失败则可能有**身体反噬**（踢燃烧瓶被烧等）→ 开 mishap 守卫，叙事后据 planner 确定性扣血。
        succeeded = result.outcome not in ("failure", "fumble")
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            KP_DICE_CONTINUATION_PROMPT.format(dice_results=desc),
            sanity_guard=succeeded,
            mishap_guard=(result.outcome == "fumble"),
        )
    except asyncio.CancelledError:
        logger.info("投骰生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("投骰生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "生成出错，请重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


def _persist_module_intro(db: Session, session_id: str, module: Module) -> str | None:
    """开场前先落一张「背景导语」卡：模组类型/年代/地区/难度/人数 + 一句话前提。

    取自模组作者填写的公开元信息（world_setting / description），不含任何线索或真相，
    给玩家一个「这是个什么故事」的定位，免得直接被拉进场景而摸不着头脑。返回卡片 chunk。
    """
    ws = module.world_setting or {}
    bits: list[str] = []
    for key in ("tone", "era", "region"):
        v = str(ws.get(key) or "").strip()
        if v:
            bits.append(v)
    diff = str(ws.get("difficulty") or "").strip()
    if diff:
        bits.append(f"难度 {diff}")
    pc = str(ws.get("player_count") or "").strip()
    if pc:
        bits.append(f"建议 {pc} 人")
    meta = " · ".join(bits)
    premise = str(module.description or "").strip()
    if not (meta or premise):
        return None
    ev = session_service.add_event(
        db, session_id, "system", premise, actor_name="系统",
        metadata={"kind": "module_intro", "title": module.title or "模组", "meta": meta},
    )
    return event_to_chunk(ev)


async def run_opening_generation(session_id: str) -> None:
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        # 幂等：已有正式叙事（旁白/对话）则不重复开场，只收尾。背景导语卡（system）不计入，
        # 这样开场生成中途失败后重试仍能重新生成（而背景卡只补一次、不重复）。
        events_all = session_service.get_session_events(db, session_id)
        if any(e.event_type in ("narration", "dialogue") for e in events_all):
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        if not any((e.metadata_ or {}).get("kind") == "module_intro" for e in events_all):
            intro_chunk = _persist_module_intro(db, session_id, module)
            if intro_chunk:
                room_hub.broadcast(session_id, intro_chunk)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        # 开场把各角色卡的静态 equipment 播种进活库存（幂等：库存非空则跳过）。
        for c in [player_char, *party_others]:
            if c:
                inventory_service.seed_from_equipment(db, c)
        # 开场场景配图卡（首入防重靠 scene_cards，开场生成失败重试也只出一张）
        for chunk in _maybe_scene_illustration(
            db, session_id, module, game_session.current_scene_id,
        ):
            room_hub.broadcast(session_id, chunk)
        # 开场不跑队友回合（尚无玩家行动），但把队伍信息带进 KP 上下文让其知道谁在场
        await _run_generation(
            db, session_id, game_session, module, player_char, [],
            teammates=party_others,
        )
    except asyncio.CancelledError:
        logger.info("开场生成被取消: session=%s", session_id)
    except Exception as e:
        logger.exception("开场生成失败: session=%s", session_id)
        # 落库系统提示（而非仅广播）：否则客户端收到 done 后 resync 会把它一并抹掉。
        # 能归类的错误给出可行动原因（如 401→检查 Key），否则回落通用文案。
        hint = _classify_llm_error(e)
        msg = (
            f"（开场生成失败：{hint}。修好后点「重试开场」即可。）"
            if hint else "（开场生成中断，请点「重试开场」或刷新。）"
        )
        _persist_error_notice(db, session_id, msg)
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def initialize_human_session(session_id: str) -> None:
    """真人 KP 开局初始化：落公开导语与首场景卡，但绝不调用 AI 生成叙事。"""
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        if not game_session or game_session.kp_mode != "human":
            return
        module = db.get(Module, game_session.module_id)
        if module is None:
            return
        events = session_service.get_session_events(db, session_id)
        if not any((e.metadata_ or {}).get("kind") == "module_intro" for e in events):
            intro = _persist_module_intro(db, session_id, module)
            if intro:
                room_hub.broadcast(session_id, intro)
        for chunk in _maybe_scene_illustration(
            db, session_id, module, game_session.current_scene_id,
        ):
            room_hub.broadcast(session_id, chunk)
        room_hub.broadcast(session_id, _make_chunk("done"))
    except asyncio.CancelledError:
        logger.info("真人 KP 开局初始化被取消: session=%s", session_id)
    except Exception:
        logger.exception("真人 KP 开局初始化失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（真人 KP 开局初始化中断，请重试）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_travel_generation(
    session_id: str, actor_id: str, scene_id: str, via: list[str] | None = None,
) -> None:
    """玩家经大地图『前往』某地：确定性切换该角色所在场景，落「前往」行动，再由 KP 叙述抵达。

    场景切换是后端据玩家显式选择执行的（非 KP 臆测），从根上杜绝「说句话就被自动搬走」。
    ``via``：连通图算出的途经场景名（目标不相邻但连通时非空）——KP 据此叙述穿行而非瞬移。
    """
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        actor = db.get(Character, actor_id) or player_char
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        ai_teammates = session_service.get_ai_teammates(db, session_id)
        scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
        scene_name = (scene or {}).get("title") or (scene or {}).get("name") or scene_id

        room_hub.broadcast(session_id, _make_chunk("generating"))
        # 确定性切换该角色位置（主角则一并更新 current_scene_id），并落一条「前往」行动
        session_service.set_char_location(db, session_id, actor.id, scene_id)
        ev = session_service.add_event(
            db, session_id, "action", f"（前往：{scene_name}）",
            actor_id=actor.id, actor_name=actor.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev))
        # 首次抵达该场景 → 场景配图卡（先出卡，KP 叙述随后跟上；图片异步补挂）
        for chunk in _maybe_scene_illustration(db, session_id, module, scene_id):
            room_hub.broadcast(session_id, chunk)

        if via:
            passage = "、".join(f"【{v}】" for v in via)
            prompt = (
                f"{actor.name} 从原处出发，途经{passage}，抵达了【{scene_name}】。"
                "途经之处一笔带过（至多点缀一两句沿途见闻，不停留、不触发事件），"
                "再描述抵达地此刻的见闻与气氛，自然承接前文；"
                "不要触发任何检定，也不要替其他玩家角色行动或代言。"
            )
        else:
            prompt = (
                f"{actor.name} 抵达了【{scene_name}】。请描述此地此刻的见闻与气氛，自然承接前文；"
                "不要触发任何检定，也不要替其他玩家角色行动或代言。"
            )
        # 前往后紧接一轮 AI 队友回合：留在原地/另处的队友据「分头」处境各自推进本场景，
        # 不再因为这条路不经 run_chat_generation 而全程哑火。
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others, prompt,
            then_team_turn=ai_teammates,
        )
    except asyncio.CancelledError:
        logger.info("前往生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("前往生成失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（前往生成中断，请重试）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_regenerate_generation(session_id: str) -> None:
    """重新生成最新一轮 KP 叙事：拿本轮玩家与 AI 队友的既有输入、以及已定的骰子作上下文，
    只重跑 KP（不重跑队友回合、不做检定意图分诊），产出新的叙事。

    调用前应已由端点：①取消卡住的旧生成 task；②回滚上一轮 KP 叙事产物
    （session_service.rollback_last_kp_output）。本函数只负责用清理后的事件流重跑 KP。
    """
    await _drain_housekeeping(session_id)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        events = session_service.get_session_events(db, session_id)
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others,
        )
    except asyncio.CancelledError:
        logger.info("重新生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("重新生成失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（重新生成中断，请重试）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


# 兼容既有调用；确定性回合副作用的单一实现位于 turn_effects。
_update_character_stat = turn_effects._update_character_stat
_apply_madness_status = turn_effects._apply_madness_status
_tick_madness_recovery = turn_effects._tick_madness_recovery
_exec_san_check = turn_effects._exec_san_check
_resolve_hp_target = turn_effects._resolve_hp_target
_heal_kind = turn_effects._heal_kind
_infer_heal_target = turn_effects._infer_heal_target
_apply_heal_on_success = turn_effects._apply_heal_on_success
_exec_hp_change = turn_effects._exec_hp_change
_exec_dice_check = turn_effects._exec_dice_check
_auto_roll_check = turn_effects._auto_roll_check
_exec_scene_change = turn_effects._exec_scene_change
_exec_flag = turn_effects._exec_flag
_exec_handout = turn_effects._exec_handout




# 兼容既有调用；真人 KP 工具桌适配位于 human_kp_actions。
execute_human_kp_action = human_kp_actions.execute_human_kp_action







# 兼容既有调用；KP 确定性动作的单一实现位于 kp_actions。
_exec_npc_act = kp_actions._exec_npc_act
_exec_start_chase = kp_actions._exec_start_chase
_exec_start_combat = kp_actions._exec_start_combat
_exec_say = kp_actions._exec_say




# 兼容既有调用；KP 工具协议与循环的单一实现位于 kp_tool_loop。
_rule_lookup_passages = kp_tool_loop._rule_lookup_passages
_module_lookup_passages = kp_tool_loop._module_lookup_passages
MAX_TOOL_LOOP_STEPS = kp_tool_loop.MAX_TOOL_LOOP_STEPS
_tool_loop_active = kp_tool_loop._tool_loop_active
_merge_step_result = kp_tool_loop._merge_step_result
_SOLO_ARG_KEY = kp_tool_loop._SOLO_ARG_KEY
_TEXT_TAG_RE = kp_tool_loop._TEXT_TAG_RE
_tool_call_from_text = kp_tool_loop._tool_call_from_text
_plan_check_call = kp_tool_loop._plan_check_call
_build_kp_tool_executor = kp_tool_loop._build_kp_tool_executor
_run_kp_agent_loop = kp_tool_loop._run_kp_agent_loop
_process_commands = kp_tool_loop._process_commands
_handle_rule_lookup = kp_tool_loop._handle_rule_lookup
_handle_module_lookup = kp_tool_loop._handle_module_lookup
