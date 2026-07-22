"""KP 工具协议、原生 agent loop 与旧文本指令兼容运行时。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai import turn_planner
from app.ai import tools as kp_tools
from app.ai.agents.kp_agent import _CHECK_TURN_TEMPERATURE, KPAgent
from app.ai.context import build_kp_context
from app.ai.prompts.kp_system import (
    KP_DICE_CONTINUATION_PROMPT,
    KP_MODULE_CONTINUATION_PROMPT,
    KP_RULE_CONTINUATION_PROMPT,
)
from app.ai.provider import ToolCall
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import (
    chat_event_writer,
    command_protocol,
    dice_runtime,
    illustration_service,
    kp_actions,
    module_rag_service,
    narration_protocol,
    rulebook_service,
    session_service,
    team_turn_service,
    turn_context,
    turn_effects,
)
from app.services.event_protocol import make_chunk as _make_chunk

logger = logging.getLogger(__name__)

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
MAX_RULE_LOOKUPS = command_protocol.MAX_RULE_LOOKUPS
MAX_DICE_CONTINUATIONS = command_protocol.MAX_DICE_CONTINUATIONS
_parse_tag_kv = command_protocol.parse_tag_kv
_filter_narration_stream = narration_protocol.filter_narration_stream
_is_party_speaker = narration_protocol._is_party_speaker
_record_chunk_event = chat_event_writer.record_chunk_event
_resolve_opposed = dice_runtime._resolve_opposed
_record_rag = turn_context._record_rag
_record_npc_say_memory = turn_context._record_npc_say_memory
_scene_name = turn_context._scene_name
_matcher_npcs = team_turn_service._matcher_npcs
_stream_narration_filtered = team_turn_service._stream_narration_filtered
_attach_npc_portrait = illustration_service._attach_npc_portrait
_exec_npc_act = kp_actions._exec_npc_act
_exec_start_chase = kp_actions._exec_start_chase
_exec_start_combat = kp_actions._exec_start_combat
_exec_say = kp_actions._exec_say
_exec_san_check = turn_effects._exec_san_check
_exec_hp_change = turn_effects._exec_hp_change
_exec_dice_check = turn_effects._exec_dice_check
_exec_scene_change = turn_effects._exec_scene_change
_exec_flag = turn_effects._exec_flag
_exec_handout = turn_effects._exec_handout


def _rule_lookup_passages(
    db: Session, query: str, rule_system: str, game_session: GameSession | None = None,
) -> str:
    """检索规则书原文并拼成回灌片段；检索不到给降级文案（fail-open）。"""
    hits = rulebook_service.retrieve(db, query, rule_system, k=3)
    _record_rag(db, game_session, kind="rule", mode="active", query=query, hits=hits)
    if hits:
        return "\n\n".join(f"[第 {h['page']} 页] {h['text']}" for h in hits)
    return "（未在规则书中找到直接匹配的内容，请依据《裁定手册》与你的经验处理。）"


def _module_lookup_passages(
    db: Session, module: Module, game_session: GameSession, query: str,
) -> str:
    """检索模组原文并拼成回灌片段；检索不到/失败给降级文案（fail-open）。"""
    try:
        hits = module_rag_service.retrieve(
            db, module.id, query, k=3, scene_id=game_session.current_scene_id,
        )
    except Exception:  # noqa: BLE001 — 检索失败降级为无命中
        logger.exception("模组原文检索失败（已降级）：module=%s", module.id)
        hits = []
    _record_rag(db, game_session, kind="module", mode="active", query=query, hits=hits)
    if hits:
        return "\n\n".join(
            f"[片段 {i}] {h['text']}" for i, h in enumerate(hits, start=1)
        )
    return "（未在模组原文中找到直接匹配的内容，请依据结构化模组资料与你的经验续写。）"


# ── KP agent loop（tool use 新路径，use_tool_calls 开关控制）──────────────────

# 单轮 loop 的步数上限：超限注入「请直接收束」再生成一次收尾，防无限工具链。
MAX_TOOL_LOOP_STEPS = 6


def _tool_loop_active(llm) -> bool:
    """KP 生成是否走 agent loop（工具调用）新路径：配置开关 && Provider 支持工具。

    开关**默认开启**（use_tool_calls=true）；无激活档/读取异常一律回退旧路径（fail-open）。
    """
    try:
        from app.api.ai_settings import load_active_profile
        profile = load_active_profile()
    except Exception:
        return False
    if not profile or not getattr(profile, "use_tool_calls", True):
        return False
    try:
        return bool(llm.supports_tools())
    except Exception:
        return False


def _merge_step_result(result: list, step: list) -> None:
    """把一步生成的产物合并进整轮聚合 result（对话/分组偏移按已累计旁白长度平移）。"""
    base = len(result[0])
    result[0] += step[0]
    result[1] += step[1]
    if len(result) > 2 and len(step) > 2:
        result[2].extend(step[2])
    if len(result) > 3 and len(step) > 3:
        result[3].extend((off + base, spk, txt) for off, spk, txt in step[3])
    if len(result) > 4 and len(step) > 4:
        result[4].extend((off + base, label) for off, label in step[4])


# 裸值容错的单参数指令（[SET_FLAG hint_x] 这类漏写键名的旧习惯）→ 对应参数键
_SOLO_ARG_KEY = {
    "set_flag": "flag", "clear_flag": "flag", "handout": "id",
    "scene_change": "scene_id", "rule_lookup": "query", "module_lookup": "query",
}

_TEXT_TAG_RE = re.compile(r"\[([A-Z_]{3,})(?:[:：\s]([^\]]*))?\]")


def _tool_call_from_text(step_text: str) -> ToolCall | None:
    """loop 兜底：模型没走工具、而是把指令写成了文本（手写 prompt 的旧习惯）——
    把第一条终止型指令解析成等价的合成 ToolCall，交给同一执行器处理。

    只认注册表里的终止型指令（GROUP/SAY 是文本标注不算动作）。
    参数解析与旧正则同款宽容：键值对优先，单参数指令允许裸值。
    """
    text = (step_text or "").replace("【", "[").replace("】", "]")
    for m in _TEXT_TAG_RE.finditer(text):
        tag, inner = m.group(1), (m.group(2) or "").strip()
        # SAY/GROUP 是文本标注、不是动作：内联 [SAY] 由台词过滤器直接抽成气泡，
        # 这里绝不能再合成一个 say() 工具调用，否则同一句台词会重复出气泡。
        if tag in ("SAY", "GROUP"):
            continue
        name = kp_tools.TAG_TO_TOOL.get(tag)
        if name is None:
            continue
        kv = _parse_tag_kv(inner)
        if not kv and inner:
            solo = _SOLO_ARG_KEY.get(name)
            if solo:
                kv = {solo: inner}
        return ToolCall(id=f"text_{uuid.uuid4().hex[:8]}", name=name, arguments=kv)
    return None


def _plan_check_call(plan: turn_planner.TurnPlan) -> ToolCall:
    """裁定轮兜底：按计划的 check 字段拼出确定性补掷的 dice_check 调用
    （与 turn_planner._check_directive 同一语义，等价 KPAgent 的补指令兜底）。"""
    check = plan.check
    args: dict = {"skill": check.skill or "侦查"}
    if check.difficulty:
        args["difficulty"] = check.difficulty
    if check.visibility and check.visibility != "open":
        args["visibility"] = check.visibility
    if check.chars:
        args["chars"] = check.chars
    return ToolCall(id=f"fallback_{uuid.uuid4().hex[:8]}", name="dice_check", arguments=args)


def _build_kp_tool_executor(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    player_char: Character, teammates: list[Character] | None, llm,
    result: list,
):
    """构建 loop 路径的工具执行器：把注册表工具名分发到上面的共用执行函数（不复制逻辑）。

    闭包内维护每轮查阅配额（rule_lookup 与 module_lookup 合计 MAX_RULE_LOOKUPS 次，
    超限返回拒绝文本——与旧路径 lookup_depth 语义一致）。未知工具名回「无此工具」结果、
    任何执行异常 fail-open 返回错误说明，绝不断流。
    """
    lookup_used = 0

    # 每个工具一个 handler(name, kv)；分发表取代旧的长 if/elif 链。ToolSpec（tools.py）仍是
    # 参数 schema 的单一事实源；此处是「工具名 → 执行」的单一事实源（独立放置以避免与
    # tools.py 循环依赖，handler 需闭包访问 db/session/module 等运行期上下文）。
    async def _h_dice_check(name, kv):
        if not kv.get("skill"):
            return kp_tools.ToolOutcome("参数缺失：skill 为必填。请带上技能名重试，或直接继续叙述。")
        chunks, descs, pending = await _exec_dice_check(
            db, session_id, game_session, module, kv, player_char, teammates,
        )
        if pending:
            return kp_tools.ToolOutcome(
                "已向该玩家发出检定请求，等待其亲自掷骰。本轮叙述就此收束，绝不预测结果。",
                chunks=chunks, suspend=True,
            )
        return kp_tools.ToolOutcome(
            KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)), chunks=chunks,
        )

    async def _h_opposed_check(name, kv):
        descs: list[str] = []
        chunks = [
            c async for c in _resolve_opposed(
                db, session_id, kv, get_engine(module.rule_system),
                module, player_char, teammates, descs,
            )
        ]
        if not descs:
            return kp_tools.ToolOutcome("参数缺失：skill（或 a_skill/b_skill）为必填。", chunks=chunks)
        return kp_tools.ToolOutcome(
            KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)), chunks=chunks,
        )

    async def _h_san_check(name, kv):
        chunks, descs = await _exec_san_check(
            db, session_id, game_session, kv, player_char, teammates,
        )
        if not descs:
            return kp_tools.ToolOutcome(
                "本次理智检定无需结算（目睹者均已对该恐怖源检定过）。", chunks=chunks,
            )
        return kp_tools.ToolOutcome(
            KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)), chunks=chunks,
        )

    async def _h_lookup(name, kv):
        nonlocal lookup_used
        if lookup_used >= MAX_RULE_LOOKUPS:
            return kp_tools.ToolOutcome(
                f"本轮查阅配额已用完（规则书与模组原文合计最多 {MAX_RULE_LOOKUPS} 次），"
                "请依据既有资料直接续写，不要再查阅。"
            )
        query = kv.get("query", "").strip()
        if not query:
            return kp_tools.ToolOutcome("参数缺失：query 为必填。")
        lookup_used += 1
        if name == "rule_lookup":
            passages = _rule_lookup_passages(db, query, module.rule_system, game_session)
            return kp_tools.ToolOutcome(
                KP_RULE_CONTINUATION_PROMPT.format(query=query, passages=passages),
                chunks=[_make_chunk("system", "守秘人翻阅规则书……")],
            )
        passages = _module_lookup_passages(db, module, game_session, query)
        return kp_tools.ToolOutcome(
            KP_MODULE_CONTINUATION_PROMPT.format(query=query, passages=passages),
            chunks=[_make_chunk("system", "守秘人翻阅模组手稿……")],
        )

    async def _h_say(name, kv):
        who = kv.get("who", "").strip()
        text = kv.get("text", "").strip().strip("“”\"「」『』")
        if not who or not text:
            return kp_tools.ToolOutcome("参数缺失：who 与 text 均为必填。")
        # 守卫：绝不用 say() 替玩家/队友说话或行动（他们的台词由本人给出）。
        party = {player_char.name} | {t.name for t in (teammates or [])}
        if _is_party_speaker(who, party):
            return kp_tools.ToolOutcome(
                f"拒绝：{who} 是玩家或队友角色，你不能替他们说话或行动。"
                "玩家与队友的言行只能由他们本人给出；你只叙述 NPC 与环境，把选择权留给他们。"
            )
        chunks = _exec_say(result, module, who, text)
        return kp_tools.ToolOutcome(
            "台词已作为气泡展示给玩家（续写时不要复述这句话）。", chunks=chunks,
        )

    async def _h_start_combat(name, kv):
        chunks = await _exec_start_combat(
            db, session_id, game_session, module, player_char, teammates, llm,
            kv.get("enemies", ""), kv.get("trigger", ""),
        )
        return kp_tools.ToolOutcome(
            "已切入结构化战斗轮，交由系统按先攻推进；本轮就此收束，战斗结束后系统会回灌结果摘要。",
            chunks=chunks, suspend=True,
        )

    async def _h_start_chase(name, kv):
        chunks = _exec_start_chase(
            db, session_id, module, player_char, kv.get("pursuer", ""), kv.get("trigger", ""),
        )
        return kp_tools.ToolOutcome(
            "已切入追逐（抽象距离轨），交由系统逐轮推进；本轮就此收束，追逐结束后系统会回灌结果。",
            chunks=chunks, suspend=True,
        )

    async def _h_npc_act(name, kv):
        npc_id = kv.get("npc_id", "").strip()
        trigger = kv.get("trigger", "").strip()
        if not npc_id or not trigger:
            return kp_tools.ToolOutcome("参数缺失：npc_id 与 trigger 均为必填。")
        chunks, response = await _exec_npc_act(
            db, session_id, game_session, module, llm, player_char, npc_id, trigger,
        )
        return kp_tools.ToolOutcome(
            f"该 NPC 已行动/开口（台词已直接展示给玩家，续写时不要复述）：{response}", chunks=chunks,
        )

    async def _h_scene_change(name, kv):
        chunks, sid, note = await _exec_scene_change(
            db, session_id, game_session, module,
            kv.get("scene_id", "").strip(), player_char, teammates,
        )
        if sid:
            return kp_tools.ToolOutcome(f"ok：场景已切换至 {_scene_name(module, sid)}", chunks=chunks)
        return kp_tools.ToolOutcome(note or "场景引用无法解析或未变化（保持当前场景）。", chunks=chunks)

    async def _h_flag(name, kv):
        flag = kv.get("flag", "").strip()
        if not flag:
            return kp_tools.ToolOutcome("参数缺失：flag 为必填。")
        chunks = _exec_flag(db, session_id, game_session, flag, name == "set_flag")
        return kp_tools.ToolOutcome("ok", chunks=chunks)

    async def _h_hp_change(name, kv):
        chunks = await _exec_hp_change(
            db, session_id, player_char,
            kv.get("target", ""), kv.get("delta", ""), kv.get("reason", ""),
            module=module, teammates=teammates,
        )
        if chunks:
            return kp_tools.ToolOutcome("ok", chunks=chunks)
        return kp_tools.ToolOutcome("未结算（target 当前仅支持 player，且 delta 须为整数）。")

    async def _h_handout(name, kv):
        hid = kv.get("id", "").strip()
        if not hid:
            return kp_tools.ToolOutcome("参数缺失：id 为必填。")
        chunks, note = await _exec_handout(
            db, session_id, game_session, module, hid, player_char, teammates,
        )
        return kp_tools.ToolOutcome(note, chunks=chunks)

    handlers = {
        "dice_check": _h_dice_check,
        "opposed_check": _h_opposed_check,
        "san_check": _h_san_check,
        "rule_lookup": _h_lookup,
        "module_lookup": _h_lookup,
        "say": _h_say,
        "start_combat": _h_start_combat,
        "start_chase": _h_start_chase,
        "npc_act": _h_npc_act,
        "scene_change": _h_scene_change,
        "set_flag": _h_flag,
        "clear_flag": _h_flag,
        "hp_change": _h_hp_change,
        "handout": _h_handout,
    }

    async def execute(call: ToolCall) -> kp_tools.ToolOutcome:
        name = call.name
        kv = {k: str(v).strip() for k, v in (call.arguments or {}).items() if v is not None}
        if kp_tools.TOOLS_BY_NAME.get(name) is None:
            return kp_tools.ToolOutcome(
                f"无此工具：{name}。只可调用系统提供的工具；若无需工具，直接继续叙述。"
            )
        handler = handlers.get(name)
        if handler is None:
            return kp_tools.ToolOutcome(
                f"工具 {name} 暂无 loop 行为（内部错误），请直接继续叙述。"
            )
        try:
            return await handler(name, kv)
        except Exception:
            logger.exception("工具执行失败: %s session=%s", name, session_id)
            return kp_tools.ToolOutcome(
                f"工具 {name} 执行出错，请不要重试该工具，直接继续叙述。"
            )

    execute._handled_tools = frozenset(handlers)   # 供测试校验：分发表须覆盖注册表全部工具
    return execute


async def _run_kp_agent_loop(
    llm, messages: list[dict], result: list, execute_tool, *,
    tools: list[dict] | None = None,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    plan: turn_planner.TurnPlan | None = None,
    max_steps: int = MAX_TOOL_LOOP_STEPS,
    party_names: set[str] | None = None,
    event_order: list | None = None,
) -> AsyncIterator[str]:
    """KP agent loop：与 _stream_narration_filtered 并列的新路径（use_tool_calls 开启时用）。

    stream_chat 流式生成：文本增量过同一套台词过滤后实时广播；tool_call 到达 →
    执行器执行 → 结果作为 role="tool" 消息回注 → 继续循环。DICE_CHECK/RULE_LOOKUP/
    MODULE_LOOKUP 由此天然取代旧路径的「续写 prompt」模式。

    - 步数上限 max_steps（默认 6）：超限注入「请直接收束本轮叙述」再无工具生成一次收尾；
    - 裁定轮（plan.requires_check）：采样降温 _CHECK_TURN_TEMPERATURE，且若模型始终没
      发起 dice_check/opposed_check，则按计划确定性补掷（等价 KPAgent 的补指令兜底）；
    - 模型把指令写成文本时（手写 prompt 旧习惯）：解析成合成 ToolCall 走同一执行器，
      [MOVE] 内联标记照旧生效，[SAY]/[GROUP] 由文本过滤器照常处理；
    - 执行器返回 suspend（如已挂「待玩家投骰」）：本轮生成就此收束。
    产物写入 result（与旧路径同构）；validator 终检、落库、世界记忆钩子、幕后推演等
    收尾由调用方与旧路径共用——loop 只替换「生成 + 指令执行」段。
    """
    tools = tools if tools is not None else kp_tools.openai_tool_schemas()
    requires_check = bool(plan is not None and plan.requires_check)
    temperature = _CHECK_TURN_TEMPERATURE if requires_check else 0.85
    messages = list(messages)  # loop 会往里回注消息，不污染调用方的列表
    did_check = False
    natural_end = False

    for _step in range(max_steps):
        step = ["", "", [], [], []]
        tool_calls: list[ToolCall] = []

        async def _text_deltas(calls=tool_calls):
            async for delta in llm.stream_chat(messages, tools=tools, temperature=temperature):
                if delta.kind == "text" and delta.text:
                    yield delta.text
                elif delta.kind == "tool_call" and delta.tool_call is not None:
                    calls.append(delta.tool_call)

        try:
            async for chunk in _filter_narration_stream(
                _text_deltas(), step, npcs=npcs, group_label=group_label,
                guess_speakers=False,  # 对话走 say() 工具；旁白里的裸引号一律留旁白、不猜
                party_names=party_names,  # 内联 [SAY] 误代言玩家/队友也挡下
            ):
                yield chunk
        except BaseException:
            _merge_step_result(result, step)  # 断流也保住已生成片段（调用方负责落库）
            raise
        _merge_step_result(result, step)

        step_text = (step[1] or "").replace("【", "[").replace("】", "]")
        if not tool_calls:
            synthetic = _tool_call_from_text(step_text)
            if synthetic is not None:
                tool_calls = [synthetic]
        if not tool_calls and requires_check and not did_check:
            # 裁定轮兜底：既没调工具也没写指令 → 确定性补掷计划指定的检定
            tool_calls = [_plan_check_call(plan)]
        if not tool_calls:
            natural_end = True
            break

        messages.append({
            "role": "assistant",
            "content": step[1] or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        })
        suspended = False
        for tc in tool_calls:
            if tc.name in ("dice_check", "opposed_check"):
                did_check = True
            outcome = await execute_tool(tc)
            for chunk in outcome.chunks:
                # 记录该工具事件在广播时的旁白偏移，供收尾把它重排回旁白中的正确位置
                if event_order is not None:
                    _record_chunk_event(event_order, chunk, len(result[0]))
                yield chunk
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": outcome.result_text,
            })
            if outcome.suspend:
                suspended = True
        if suspended:
            return

    if not natural_end:
        # 步数超限：注入收束指令，最后一次不带工具生成收尾
        messages.append({
            "role": "system",
            "content": (
                "（工具调用步数已达上限）请不要再调用任何工具、也不要输出任何指令，"
                "直接收束本轮叙述：自然交代当前处境，停在等待玩家行动处。"
            ),
        })
        step = ["", "", [], [], []]

        async def _plain_deltas():
            async for delta in llm.stream_chat(messages, tools=None, temperature=temperature):
                if delta.kind == "text" and delta.text:
                    yield delta.text

        try:
            async for chunk in _filter_narration_stream(
                _plain_deltas(), step, npcs=npcs, group_label=group_label,
                guess_speakers=False, party_names=party_names,
            ):
                yield chunk
        except BaseException:
            _merge_step_result(result, step)
            raise
        _merge_step_result(result, step)


async def _process_commands(
    db: Session,
    session_id: str,
    kp_text: str,
    module: Module,
    player_char: Character,
    game_session: GameSession,
    llm,
    teammates: list[Character] | None = None,
    allow_rule_lookup: bool = True,
    lookup_depth: int = 0,
    dice_depth: int = 0,
) -> AsyncIterator[str]:
    # 全角括号归一为半角：模型有时用【】写指令，统一成 [] 好让下面各指令正则命中并处理（而非泄漏）。
    kp_text = (kp_text or "").replace("【", "[").replace("】", "]")
    # 规则书查阅是终止性指令（独占一次回复的最后一行）：命中即查阅+续写，不再处理本段其余
    if allow_rule_lookup and lookup_depth < MAX_RULE_LOOKUPS:
        lookup = RULE_LOOKUP_RE.search(kp_text)
        if lookup:
            async for chunk in _handle_rule_lookup(
                db, session_id, lookup.group(1).strip(), module, player_char,
                game_session, llm, teammates=teammates, lookup_depth=lookup_depth,
            ):
                yield chunk
            return

    # 模组原文查阅同为终止性指令，与规则书查阅走同一开关与配额（lookup_depth 合并计数）
    if allow_rule_lookup and lookup_depth < MAX_RULE_LOOKUPS:
        mlookup = MODULE_LOOKUP_RE.search(kp_text)
        if mlookup:
            async for chunk in _handle_module_lookup(
                db, session_id, mlookup.group(1).strip(), module, player_char,
                game_session, llm, teammates=teammates, lookup_depth=lookup_depth,
            ):
                yield chunk
            return

    dice_descriptions: list[str] = []

    for match in SAN_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        san_chunks, san_descs = await _exec_san_check(
            db, session_id, game_session, kv, player_char, teammates,
        )
        for chunk in san_chunks:
            yield chunk
        dice_descriptions.extend(san_descs)

    for match in HP_CHANGE_RE.finditer(kp_text):
        hp_chunks = await _exec_hp_change(
            db, session_id, player_char,
            match.group(1).strip(), match.group(2).strip(), match.group(3).strip(),
            module=module, teammates=teammates,
        )
        for chunk in hp_chunks:
            yield chunk

    engine = get_engine(module.rule_system)

    for match in DICE_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        dice_chunks, dice_descs, _pending = await _exec_dice_check(
            db, session_id, game_session, module, kv, player_char, teammates,
        )
        for chunk in dice_chunks:
            yield chunk
        dice_descriptions.extend(dice_descs)

    for match in OPPOSED_CHECK_RE.finditer(kp_text):
        try:
            async for chunk in _resolve_opposed(
                db, session_id, _parse_tag_kv(match.group(1)),
                engine, module, player_char, teammates, dice_descriptions,
            ):
                yield chunk
        except ValueError as error:
            # 模型生成的内部指令可能缺字段；跳过坏指令，不中断已经生成的叙事。
            logger.warning("跳过无效的 AI 对抗检定指令：%s", error)

    if dice_descriptions:
        continuation_prompt = KP_DICE_CONTINUATION_PROMPT.format(
            dice_results="\n".join(dice_descriptions)
        )
        events = session_service.get_session_events(db, session_id)
        messages = build_kp_context(
            game_session, module, player_char, events, teammates=teammates,
        )
        messages.append({"role": "user", "content": continuation_prompt})

        kp = KPAgent(llm)
        cont_result = ["", "", [], [], []]
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
            ):
                yield chunk
        finally:
            cont_narration = cont_result[0].rstrip()
            if cont_narration:
                session_service.add_event(
                    db, session_id, "narration", cont_narration, actor_name="KP",
                )
            for npc_name, dialogue_text in cont_result[2]:
                ev = session_service.add_event(
                    db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
                )
                _attach_npc_portrait(db, session_id, module, ev)
        # 世界记忆钩子 c：续写里的 NPC 台词同样记入其互动史
        _record_npc_say_memory(
            db, session_id, game_session, module, cont_result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
        )
        # 续写里 KP 可能再发指令（如读懂禁忌知识后追加 [SAN_CHECK]、或场景切换）——
        # 继续处理，但限深度防无限掷骰链。
        if dice_depth + 1 < MAX_DICE_CONTINUATIONS:
            async for chunk in _process_commands(
                db, session_id, cont_result[1], module, player_char, game_session, llm,
                teammates=teammates, allow_rule_lookup=False, dice_depth=dice_depth + 1,
            ):
                yield chunk

    for match in SCENE_CHANGE_RE.finditer(kp_text):
        scene_chunks, _sid, _note = await _exec_scene_change(
            db, session_id, game_session, module, match.group(1).strip(),
            player_char, teammates,
        )
        for chunk in scene_chunks:
            yield chunk

    # 剧情状态推进：置/清标志后，刷新内存里的 game_session.world_state，使本次生成的后续
    # 处理（续写、NPC 行动）与下一轮上下文都能看到最新状态。
    for match in SET_FLAG_RE.finditer(kp_text):
        for chunk in _exec_flag(db, session_id, game_session, match.group(1).strip(), True):
            yield chunk
    for match in CLEAR_FLAG_RE.finditer(kp_text):
        for chunk in _exec_flag(db, session_id, game_session, match.group(1).strip(), False):
            yield chunk

    # 手书发放：[HANDOUT: id=xxx] → 把模组手书的原文落库为 system 事件并广播（handout 是
    # 给玩家看的实体文书，正常进聊天流，前端按 metadata.kind 渲染成信笺卡片）。
    # 幂等：同 id 只发放一次（重复发放静默跳过）；未知 id 静默跳过（只记日志，不出卡片）。
    for match in HANDOUT_RE.finditer(kp_text):
        inner = match.group(1).strip()
        hid = (_parse_tag_kv(inner).get("id") or inner).strip()
        handout_chunks, _note = await _exec_handout(
            db, session_id, game_session, module, hid, player_char, teammates,
        )
        for chunk in handout_chunks:
            yield chunk

    for match in NPC_ACT_RE.finditer(kp_text):
        npc_chunks, _resp = await _exec_npc_act(
            db, session_id, game_session, module, llm, player_char,
            match.group(1).strip(), match.group(2).strip(),
        )
        for chunk in npc_chunks:
            yield chunk


async def _handle_rule_lookup(
    db: Session,
    session_id: str,
    query: str,
    module: Module,
    player_char: Character,
    game_session: GameSession,
    llm,
    teammates: list[Character] | None = None,
    lookup_depth: int = 0,
) -> AsyncIterator[str]:
    """KP 发起 [RULE_LOOKUP] 后：检索规则书原文 → 回灌让 KP 据此续写裁定。

    透明提示一条 ephemeral system（不落库）；检索不到时给降级文案让 KP 凭经验处理。
    续写产物再过一遍 _process_commands（禁再查阅），以便"查完规则随即发起检定"成立。
    """
    yield _make_chunk("system", "守秘人翻阅规则书……")

    passages = _rule_lookup_passages(db, query, module.rule_system, game_session)
    continuation = KP_RULE_CONTINUATION_PROMPT.format(query=query, passages=passages)
    events = session_service.get_session_events(db, session_id)
    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=False,  # 续写阶段不再广告查阅，避免长链
    )
    messages.append({"role": "user", "content": continuation})

    kp = KPAgent(llm)
    cont_result = ["", "", [], [], []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
        ):
            yield chunk
    finally:
        cont_narration = cont_result[0].rstrip()
        if cont_narration:
            session_service.add_event(
                db, session_id, "narration", cont_narration, actor_name="KP",
            )
        for npc_name, dialogue_text in cont_result[2]:
            ev = session_service.add_event(
                db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
            )
            _attach_npc_portrait(db, session_id, module, ev)

    # 世界记忆钩子 c：续写里的 NPC 台词同样记入其互动史
    _record_npc_say_memory(
        db, session_id, game_session, module, cont_result[2],
        [player_char.name] + [t.name for t in (teammates or [])],
    )

    # 续写里可能含查完规则后发起的检定/场景切换等，照常处理（但禁止再次查阅）
    async for chunk in _process_commands(
        db, session_id, cont_result[1], module, player_char, game_session, llm,
        teammates=teammates, allow_rule_lookup=False, lookup_depth=lookup_depth + 1,
    ):
        yield chunk


async def _handle_module_lookup(
    db: Session,
    session_id: str,
    query: str,
    module: Module,
    player_char: Character,
    game_session: GameSession,
    llm,
    teammates: list[Character] | None = None,
    lookup_depth: int = 0,
) -> AsyncIterator[str]:
    """KP 发起 [MODULE_LOOKUP] 后：检索模组原文 → 回灌让 KP 据此续写。

    与 [RULE_LOOKUP] 同一套处理模式，且共享 lookup_depth 配额（合并计数）。
    透明提示一条 ephemeral system（不落库）；检索不到/失败时给降级文案让 KP
    按结构化模组资料续写（fail-open，不阻塞跑团）。
    """
    yield _make_chunk("system", "守秘人翻阅模组手稿……")

    passages = _module_lookup_passages(db, module, game_session, query)
    continuation = KP_MODULE_CONTINUATION_PROMPT.format(query=query, passages=passages)
    events = session_service.get_session_events(db, session_id)
    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=False,  # 续写阶段不再广告查阅，避免长链
    )
    messages.append({"role": "user", "content": continuation})

    kp = KPAgent(llm)
    cont_result = ["", "", [], [], []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
        ):
            yield chunk
    finally:
        cont_narration = cont_result[0].rstrip()
        if cont_narration:
            session_service.add_event(
                db, session_id, "narration", cont_narration, actor_name="KP",
            )
        for npc_name, dialogue_text in cont_result[2]:
            ev = session_service.add_event(
                db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
            )
            _attach_npc_portrait(db, session_id, module, ev)

    # 续写里可能含查完原文后发起的检定/场景切换等，照常处理（但禁止再次查阅）
    async for chunk in _process_commands(
        db, session_id, cont_result[1], module, player_char, game_session, llm,
        teammates=teammates, allow_rule_lookup=False, lookup_depth=lookup_depth + 1,
    ):
        yield chunk
