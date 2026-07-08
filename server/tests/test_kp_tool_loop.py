"""KP agent loop（tool use 新路径）的单测：不调真实 LLM。

覆盖：工具执行与结果回注顺序、步数上限强制收束、rule/module 查阅配额、
裁定轮兜底补掷（等价 KPAgent 补指令）、开关分流新旧路径、未知工具名不崩、
文本指令兜底（模型没走工具而写了方括号指令）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import tools as kp_tools
from app.ai.agents.kp_agent import _CHECK_TURN_TEMPERATURE
from app.ai.provider import LLMProvider, StreamDelta, ToolCall
from app.ai.turn_planner import CheckPlan, TurnPlan
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401 — 注册建表
from app.services import chat_service, session_service


# ── 测试基建 ──────────────────────────────────────────────────────────────


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db) -> tuple[str, GameSession, Module, Character]:
    module = Module(
        title="测试模组", rule_system="coc", npcs=[],
        scenes=[{"id": "scene_a", "name": "书房"}, {"id": "scene_b", "name": "地下室"}],
    )
    hero = Character(name="伊芙琳", rule_system="coc", is_player=True,
                     skills={"侦查": 60, "心理学": 70})
    db.add_all([module, hero])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id, status="active",
        current_scene_id="scene_a", world_state={},
    )
    db.add(session)
    db.commit()
    return session.id, session, module, hero


class _FakeToolLLM(LLMProvider):
    """按预设脚本回放 StreamDelta 序列的 Provider；记录每次调用的入参。"""

    def __init__(self, scripts: list[list[StreamDelta]]):
        self.scripts = scripts
        self.calls: list[dict] = []          # stream_chat 的调用记录
        self.stream_calls: list[list[dict]] = []  # 旧路径 stream 的调用记录

    def supports_tools(self) -> bool:
        return True

    async def complete(self, messages, temperature=0.7, max_tokens=None, response_format=None):
        return ""

    async def stream(self, messages, temperature=0.7, max_tokens=None):
        self.stream_calls.append(messages)
        yield "旧路径叙事。"

    async def stream_chat(self, messages, tools=None, temperature=0.7, max_tokens=None):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
            "temperature": temperature,
        })
        script = self.scripts[min(len(self.calls) - 1, len(self.scripts) - 1)]
        for delta in script:
            yield delta


def _text(t: str) -> StreamDelta:
    return StreamDelta(kind="text", text=t)


def _call(name: str, args: dict, cid: str = "call_1") -> StreamDelta:
    return StreamDelta(kind="tool_call", tool_call=ToolCall(id=cid, name=name, arguments=args))


class _RecordingExecutor:
    """记录收到的 ToolCall 并按剧本返回 ToolOutcome 的假执行器。"""

    def __init__(self, outcomes: dict[str, kp_tools.ToolOutcome] | None = None):
        self.received: list[ToolCall] = []
        self.outcomes = outcomes or {}

    async def __call__(self, call: ToolCall) -> kp_tools.ToolOutcome:
        self.received.append(call)
        return self.outcomes.get(call.name, kp_tools.ToolOutcome("ok"))


async def _run_loop(llm, executor, plan=None, max_steps=6, messages=None) -> tuple[list, list[str]]:
    result = ["", "", [], [], []]
    chunks = [
        c async for c in chat_service._run_kp_agent_loop(
            llm, messages or [{"role": "system", "content": "KP"}], result, executor,
            plan=plan, max_steps=max_steps,
        )
    ]
    return result, chunks


# ── 工具执行与回注顺序 ────────────────────────────────────────────────────


def test_tool_result_feedback_order():
    """tool_call 到达 → 执行器执行 → 结果以 role=tool 回注 → 继续生成；
    文本与工具结果的顺序、聚合叙事都要对。"""
    llm = _FakeToolLLM([
        [_text("你俯身敲击。"), _call("set_flag", {"flag": "f1"})],
        [_text("尘埃落定。")],
    ])
    executor = _RecordingExecutor({
        "set_flag": kp_tools.ToolOutcome(
            "ok", chunks=[chat_service._make_chunk("system", "剧情推进：f1")],
        ),
    })
    result, chunks = asyncio.run(_run_loop(llm, executor))

    # 执行器收到且只收到这一次调用
    assert [c.name for c in executor.received] == ["set_flag"]
    assert executor.received[0].arguments == {"flag": "f1"}
    # 第二次生成的上下文里：assistant(tool_calls) 在前、tool 结果紧随其后
    second_messages = llm.calls[1]["messages"]
    assistant = next(m for m in second_messages if m.get("tool_calls"))
    idx = second_messages.index(assistant)
    assert assistant["tool_calls"][0]["function"]["name"] == "set_flag"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"flag": "f1"}
    tool_msg = second_messages[idx + 1]
    assert tool_msg["role"] == "tool" and tool_msg["content"] == "ok"
    assert tool_msg["tool_call_id"] == "call_1"
    # 两步文本聚合进同一 result（validator/落库直接复用旧路径收尾）
    assert result[0] == "你俯身敲击。尘埃落定。"
    # 广播顺序：step1 旁白 → 工具的 system chunk → step2 旁白
    kinds = [json.loads(c[len("data: "):])["type"] for c in chunks]
    assert kinds == ["narration", "system", "narration"]


def test_loop_natural_end_single_step():
    """无工具调用时一步自然收束：llm 只被调一次，无回注消息。"""
    llm = _FakeToolLLM([[_text("门厅里灰尘浮动。")]])
    executor = _RecordingExecutor()
    result, _ = asyncio.run(_run_loop(llm, executor))
    assert len(llm.calls) == 1
    assert executor.received == []
    assert result[0] == "门厅里灰尘浮动。"


# ── 步数上限 ─────────────────────────────────────────────────────────────


def test_step_cap_forces_wrap_up():
    """连续 6 步都发工具调用 → 注入「收束」指令、第 7 次不带工具生成收尾。"""
    scripts = [[_call("set_flag", {"flag": f"f{i}"}, cid=f"c{i}")] for i in range(6)]
    scripts.append([_text("夜色沉了下来。")])
    llm = _FakeToolLLM(scripts)
    executor = _RecordingExecutor()
    result, _ = asyncio.run(_run_loop(llm, executor))

    assert len(executor.received) == 6          # 只执行了上限内的 6 次
    assert len(llm.calls) == 7                  # 6 步 + 1 次强制收尾
    assert llm.calls[6]["tools"] is None        # 收尾不带工具
    last_sys = [m for m in llm.calls[6]["messages"] if m["role"] == "system"][-1]
    assert "收束" in last_sys["content"]
    assert result[0].endswith("夜色沉了下来。")


# ── 查阅配额（真实执行器）─────────────────────────────────────────────────


def test_lookup_quota_shared_between_rule_and_module(db_factory):
    """rule_lookup 与 module_lookup 合计每轮最多 2 次，超限执行器返回拒绝文本。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [], llm=None, result=["", "", [], [], []],
    )

    async def _go():
        r1 = await execute(ToolCall(id="a", name="rule_lookup", arguments={"query": "擒抱"}))
        r2 = await execute(ToolCall(id="b", name="module_lookup", arguments={"query": "书房"}))
        r3 = await execute(ToolCall(id="c", name="rule_lookup", arguments={"query": "追逐"}))
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(_go())
    assert "查阅规则书" in r1.result_text          # 正常回灌（空库走降级文案）
    assert "模组原文" in r2.result_text
    assert "配额已用完" in r3.result_text          # 第 3 次被拒
    assert not r3.chunks                          # 拒绝时不再出「翻阅」提示


# ── 裁定轮兜底补掷 ────────────────────────────────────────────────────────


def test_requires_check_fallback_rolls_deterministically(db_factory):
    """裁定轮（requires_check）降温 0.2；模型既没调工具也没写指令 →
    确定性补掷计划指定的检定（真人明骰 → 挂「待玩家投骰」并就此收束）。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    plan = TurnPlan(requires_check=True, check=CheckPlan(skill="侦查", difficulty="normal"))
    llm = _FakeToolLLM([[_text("你俯身叩击书桌侧板。")]])
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [], llm=None, result=["", "", [], [], []],
    )

    result = ["", "", [], [], []]

    async def _go():
        return [
            c async for c in chat_service._run_kp_agent_loop(
                llm, [{"role": "system", "content": "KP"}], result, execute, plan=plan,
            )
        ]

    chunks = asyncio.run(_go())

    assert llm.calls[0]["temperature"] == _CHECK_TURN_TEMPERATURE  # 裁定轮降温
    assert len(llm.calls) == 1                    # 补掷挂起后不再继续生成
    assert any('"check_request"' in c for c in chunks)  # 已广播「待玩家投骰」
    pending = (db.get(GameSession, session_id).world_state or {}).get("pending_checks") or {}
    assert pending and list(pending.values())[0]["skill"] == "侦查"


def test_requires_check_model_calls_tool_no_double_roll(db_factory):
    """模型自己调了 dice_check：不再兜底补掷（只挂一次待投骰）。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    plan = TurnPlan(requires_check=True, check=CheckPlan(skill="侦查"))
    llm = _FakeToolLLM([
        [_text("你凑近细看。"), _call("dice_check", {"skill": "侦查", "difficulty": "normal"})],
    ])
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [], llm=None, result=["", "", [], [], []],
    )
    result = ["", "", [], [], []]

    async def _go():
        return [
            c async for c in chat_service._run_kp_agent_loop(
                llm, [{"role": "system", "content": "KP"}], result, execute, plan=plan,
            )
        ]

    chunks = asyncio.run(_go())
    assert len(llm.calls) == 1
    assert sum(1 for c in chunks if '"check_request"' in c) == 1
    pending = (db.get(GameSession, session_id).world_state or {}).get("pending_checks") or {}
    assert len(pending) == 1


# ── 文本指令兜底 ──────────────────────────────────────────────────────────


def test_text_command_fallback_executes_via_registry():
    """模型没走工具、把指令写成文本（[SET_FLAG hint_x]）→ 解析成合成 ToolCall
    交同一执行器；指令文本不泄漏进旁白。"""
    llm = _FakeToolLLM([
        [_text("她说完那句话。\n\n[SET_FLAG hint_x]")],
        [_text("空气骤然安静。")],
    ])
    executor = _RecordingExecutor()
    result, _ = asyncio.run(_run_loop(llm, executor))
    assert [c.name for c in executor.received] == ["set_flag"]
    assert executor.received[0].arguments == {"flag": "hint_x"}
    assert "SET_FLAG" not in result[0] and "hint_x" not in result[0]
    assert "空气骤然安静" in result[0]


# ── 未知工具名 ────────────────────────────────────────────────────────────


def test_unknown_tool_returns_error_result_no_crash(db_factory):
    """未知工具名：执行器回「无此工具」结果，loop 正常继续、不崩。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [], llm=None, result=["", "", [], [], []],
    )
    llm = _FakeToolLLM([
        [_call("bogus_tool", {"x": 1})],
        [_text("叙述继续。")],
    ])
    result = ["", "", [], [], []]

    async def _go():
        return [
            c async for c in chat_service._run_kp_agent_loop(
                llm, [{"role": "system", "content": "KP"}], result, execute,
            )
        ]

    asyncio.run(_go())
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "无此工具" in tool_msgs[0]["content"]
    assert result[0] == "叙述继续。"


# ── 开关分流 ─────────────────────────────────────────────────────────────


class _Profile:
    def __init__(self, use_tool_calls: bool):
        self.use_tool_calls = use_tool_calls


def _patch_runtime(monkeypatch, db_factory, llm):
    import app.database as database
    from app.services.room_hub import room_hub

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_service, "get_llm", lambda: llm)
    monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)


def test_switch_off_uses_legacy_path(db_factory, monkeypatch):
    """开关关闭（默认）：走旧路径（KPAgent.stream），不碰 stream_chat。"""
    import app.api.ai_settings as ai_settings

    llm = _FakeToolLLM([[_text("不该被用到。")]])
    _patch_runtime(monkeypatch, db_factory, llm)
    monkeypatch.setattr(ai_settings, "load_active_profile", lambda: _Profile(False))

    db = db_factory()
    session_id, session, module, hero = _seed(db)
    session_service.add_event(db, session_id, "action", "我环顾四周", actor_name="伊芙琳")
    events = session_service.get_session_events(db, session_id)

    asyncio.run(chat_service._run_generation(db, session_id, session, module, hero, events))

    assert llm.calls == []            # stream_chat（loop 路径）未被调用
    assert llm.stream_calls           # 旧路径 stream 被调用
    narrs = [e for e in session_service.get_session_events(db_factory(), session_id)
             if e.event_type == "narration"]
    assert narrs and narrs[-1].content == "旧路径叙事。"


def test_switch_on_uses_agent_loop(db_factory, monkeypatch):
    """开关开启且 Provider 支持工具：走 agent loop（stream_chat + 工具清单 + 工具模式提示）。"""
    import app.api.ai_settings as ai_settings

    llm = _FakeToolLLM([[_text("loop 路径叙事。")]])
    _patch_runtime(monkeypatch, db_factory, llm)
    monkeypatch.setattr(ai_settings, "load_active_profile", lambda: _Profile(True))

    db = db_factory()
    session_id, session, module, hero = _seed(db)
    session_service.add_event(db, session_id, "action", "我环顾四周", actor_name="伊芙琳")
    events = session_service.get_session_events(db, session_id)

    asyncio.run(chat_service._run_generation(db, session_id, session, module, hero, events))

    assert llm.stream_calls == []     # 旧路径未被调用
    assert len(llm.calls) == 1        # loop 路径一步自然收束
    tool_names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "dice_check" in tool_names
    # 未挂规则书 / 原文索引未就绪：对应查阅工具不提供（镜像旧路径不广告）
    assert "rule_lookup" not in tool_names and "module_lookup" not in tool_names
    sys_texts = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "system"]
    assert any("工具调用模式" in t for t in sys_texts)
    narrs = [e for e in session_service.get_session_events(db_factory(), session_id)
             if e.event_type == "narration"]
    assert narrs and narrs[-1].content == "loop 路径叙事。"


def test_say_tool_interleaves_dialogue_in_persist_order(db_factory):
    """say() 工具：台词经 result 交错标记记录（不在 loop 中直接落库），_persist_narration
    收尾时按偏移与旁白交错落库——resync 顺序为 旁白→台词→旁白，而非台词抢到旁白之前。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    result = ["", "", [], [], []]
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [], llm=None, result=result,
    )
    llm = _FakeToolLLM([
        [_text("门开了。"), _call("say", {"who": "管家", "text": "欢迎光临。"})],
        [_text("他转身离去。")],
    ])
    chunks = asyncio.run(_collect_loop(llm, result, execute))

    # 台词记入交错标记与 extracted（供世界记忆），偏移＝首段旁白长度；loop 内未直接落库
    assert result[3] == [(len("门开了。"), "管家", "欢迎光临。")]
    assert ("管家", "欢迎光临。") in result[2]
    assert session_service.get_session_events(db, session_id) == []

    # 落库后事件顺序正确交错
    chat_service._persist_narration(db, session_id, result)
    evs = [(e.event_type, e.content)
           for e in session_service.get_session_events(db, session_id)]
    assert evs == [
        ("narration", "门开了。"),
        ("dialogue", "欢迎光临。"),
        ("narration", "他转身离去。"),
    ]
    # 广播里含 dialogue 气泡（实时也能看到）
    kinds = [json.loads(c[len("data: "):])["type"] for c in chunks]
    assert "dialogue" in kinds


def test_say_tool_refuses_to_voice_player_or_teammate(db_factory):
    """守卫：say() 归到玩家/队友名下时驳回、不出气泡；对在场 NPC 正常出气泡。"""
    db = db_factory()
    session_id, session, module, hero = _seed(db)
    ally = Character(name="亨利·卡特", rule_system="coc", is_player=False)
    module.npcs = [{"id": "npc_knott", "name": "史蒂芬·诺特先生"}]
    db.add(ally)
    db.commit()
    result = ["", "", [], [], []]
    execute = chat_service._build_kp_tool_executor(
        db, session_id, session, module, hero, [ally], llm=None, result=result,
    )

    async def _go():
        rp = await execute(ToolCall(id="1", name="say",
                                    arguments={"who": "伊芙琳·哈特", "text": "我们走吧。"}))
        ra = await execute(ToolCall(id="2", name="say",
                                    arguments={"who": "亨利", "text": "跟上。"}))
        rn = await execute(ToolCall(id="3", name="say",
                                    arguments={"who": "史蒂芬·诺特先生", "text": "欢迎光临。"}))
        return rp, ra, rn

    rp, ra, rn = asyncio.run(_go())
    assert "拒绝" in rp.result_text and not rp.chunks       # 玩家：驳回、无气泡
    assert "拒绝" in ra.result_text and not ra.chunks       # 队友（名字片段）：驳回
    assert rn.chunks and result[3] == [(0, "史蒂芬·诺特先生", "欢迎光临。")]  # NPC：正常出气泡


async def _collect_loop(llm, result, execute) -> list[str]:
    return [
        c async for c in chat_service._run_kp_agent_loop(
            llm, [{"role": "system", "content": "KP"}], result, execute,
        )
    ]


# ── 注册表完整性 ──────────────────────────────────────────────────────────


def test_registry_covers_all_regex_commands():
    """注册表收编全部终止型指令 + say（结构化对话工具）；GROUP 仍是纯文本标注不入表。"""
    tags = {spec.tag for spec in kp_tools.REGISTRY}
    assert tags == {
        "DICE_CHECK", "OPPOSED_CHECK", "SAN_CHECK", "HP_CHANGE", "NPC_ACT",
        "SCENE_CHANGE", "RULE_LOOKUP", "MODULE_LOOKUP", "SET_FLAG", "CLEAR_FLAG",
        "HANDOUT", "SAY",
    }
    assert "GROUP" not in tags
    required = {spec.name: spec.parameters["required"] for spec in kp_tools.REGISTRY}
    assert required["dice_check"] == ["skill"]
    assert required["say"] == ["who", "text"]
    assert required["npc_act"] == ["npc_id", "trigger"]
    assert required["rule_lookup"] == ["query"]
    assert required["module_lookup"] == ["query"]
    assert required["set_flag"] == ["flag"]
    assert required["handout"] == ["id"]
    # OpenAI schema 形态 + exclude 裁剪
    schemas = kp_tools.openai_tool_schemas(exclude={"rule_lookup"})
    names = [s["function"]["name"] for s in schemas]
    assert "rule_lookup" not in names and "dice_check" in names
    assert all(s["type"] == "function" for s in schemas)
