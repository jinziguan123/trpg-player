"""[RULE_LOOKUP] 按需规则书查阅的集成回归（不依赖真实 LLM / 嵌入）。

覆盖：上下文按是否挂载规则书广告该能力、_process_commands 把 RULE_LOOKUP
当终止性指令路由并短路其余、_handle_rule_lookup 检索→回灌→续写落库与降级、
以及规则书按回合类型的被动注入（_rule_excerpts_for_context + 规则要点小节）。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.ai.turn_planner import CheckPlan, TurnPlan
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.services import chat_service, rulebook_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[], clues=[])
    char = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, char])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=char.id, status="active",
    )
    db.add(session)
    db.commit()
    session_service.add_event(
        db, session.id, "dialogue", "我尝试搬开石板", actor_id=char.id, actor_name=char.name,
    )
    return module, char, session


async def _collect(agen):
    return [c async for c in agen]


def test_context_advertises_rule_lookup_only_when_enabled(db_factory):
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    on = ctx.build_kp_context(
        session, module, char, events, rules_lookup_enabled=True,
    )[0]["content"]
    off = ctx.build_kp_context(
        session, module, char, events, rules_lookup_enabled=False,
    )[0]["content"]

    assert "[RULE_LOOKUP" in on
    assert "[RULE_LOOKUP" not in off


def test_process_commands_routes_and_short_circuits(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed(db)

    seen = {}

    async def fake_handle(db_, session_id, query, *a, **k):
        seen["query"] = query
        yield chat_service._make_chunk("system", "stub")

    monkeypatch.setattr(chat_service, "_handle_rule_lookup", fake_handle)

    # 文本里同时含 DICE_CHECK：RULE_LOOKUP 为终止性指令，应短路、不掷骰
    text = (
        "我需要确认一下规则。\n"
        "[RULE_LOOKUP: query=孤注一掷的后果]\n"
        "[DICE_CHECK: skill=侦查, difficulty=normal]"
    )
    chunks = asyncio.run(_collect(
        chat_service._process_commands(db, session.id, text, module, char, session, None)
    ))

    assert seen["query"] == "孤注一掷的后果"
    assert not any('"dice"' in c for c in chunks)
    dice_events = [
        e for e in session_service.get_session_events(db, session.id)
        if e.event_type == "dice"
    ]
    assert dice_events == []  # 短路：没有触发检定


def test_handle_rule_lookup_retrieves_continues_and_persists(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed(db)

    captured = {}

    def fake_retrieve(db_, q, rule_system, k=3):
        captured["q"] = q
        captured["rs"] = rule_system
        return [{"text": "孤注一掷失败后果由守秘人加重", "page": 42, "score": 0.9, "rulebook_id": "x"}]

    monkeypatch.setattr(rulebook_service, "retrieve", fake_retrieve)

    async def fake_stream(kp, messages, result, npcs=None):
        captured["messages"] = messages
        result[0] = "据规则，你这次重掷失败，后果显著加重。"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    chunks = asyncio.run(_collect(
        chat_service._handle_rule_lookup(
            db, session.id, "孤注一掷", module, char, session, None,
        )
    ))

    assert any("翻阅规则书" in c for c in chunks)       # 透明提示
    assert captured["q"] == "孤注一掷" and captured["rs"] == "coc"

    # 规则原文（含页码）回灌进了续写上下文
    last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]["content"]
    assert "第 42 页" in last_user and "孤注一掷" in last_user

    # 续写叙事落库
    narrs = [
        e for e in session_service.get_session_events(db, session.id)
        if e.event_type == "narration"
    ]
    assert any("后果显著加重" in e.content for e in narrs)


def test_handle_rule_lookup_fallback_when_no_hits(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed(db)

    monkeypatch.setattr(rulebook_service, "retrieve", lambda *a, **k: [])

    captured = {}

    async def fake_stream(kp, messages, result, npcs=None):
        captured["messages"] = messages
        result[0] = "依经验裁定继续。"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    asyncio.run(_collect(
        chat_service._handle_rule_lookup(
            db, session.id, "某条冷门规则", module, char, session, None,
        )
    ))

    last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]["content"]
    assert "未在规则书中找到" in last_user  # 检索不到 → 降级文案


# ---------- 规则书按回合类型被动注入 ----------

def _make_event(content, seq=1):
    return EventLog(
        session_id="s", sequence_num=seq, event_type="narration",
        actor_name="KP", content=content,
    )


def test_rule_excerpts_query_mapping_by_turn_kind(db_factory, monkeypatch):
    """turn_kind → 规则术语 query 的映射（动作无规则关键词时即纯 turn_kind 术语）；
    roleplay/mixed 无映射 → 兜底用玩家最近发言原文，仍然检索（每轮必查）。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    monkeypatch.setattr(rulebook_service, "has_rulebook", lambda *a, **k: True)
    captured = {}

    def fake_retrieve(db_, q, rule_system, k=3):
        captured["q"], captured["k"] = q, k
        return [{"text": "条文片段", "page": 1, "score": 0.9, "rulebook_id": "x"}]

    monkeypatch.setattr(rulebook_service, "retrieve", fake_retrieve)

    expected = {
        "combat": "战斗 轮次 伤害 护甲",
        "investigate": "线索 检定 困难等级",
        "knowledge": "线索 检定 困难等级",
        "social": "社交 话术 取悦 恐吓 对抗",
        "move": "追逐 攀爬 跳跃",
    }
    for kind, query in expected.items():
        plan = TurnPlan(turn_kind=kind)
        hits = chat_service._rule_excerpts_for_context(db, module, plan, events)
        assert hits and hits[0]["text"] == "条文片段", kind
        assert captured["q"] == query, kind
        assert captured["k"] == 3  # top-3 控制注入体量

    # roleplay / mixed：无术语映射 → 兜底用玩家最近发言原文检索（不再整轮跳过）
    for kind in ("roleplay", "mixed"):
        captured.clear()
        hits = chat_service._rule_excerpts_for_context(
            db, module, TurnPlan(turn_kind=kind), events,
        )
        assert hits and captured["q"] == "我尝试搬开石板", kind


def test_rule_excerpts_san_context_overrides_turn_kind(db_factory, monkeypatch):
    """理智/疯狂情境优先：plan 检定涉理智，或最近事件刚发生理智结算 → 改查疯狂规则。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    monkeypatch.setattr(rulebook_service, "has_rulebook", lambda *a, **k: True)
    captured = {}

    def fake_retrieve(db_, q, rule_system, k=3):
        captured["q"] = q
        return [{"text": "疯狂条文", "page": 7, "score": 0.9, "rulebook_id": "x"}]

    monkeypatch.setattr(rulebook_service, "retrieve", fake_retrieve)

    # plan 的检定涉及理智
    plan = TurnPlan(turn_kind="investigate", check=CheckPlan(skill="理智"))
    chat_service._rule_excerpts_for_context(db, module, plan, events)
    assert captured["q"] == "疯狂 症状 恐惧"

    # 最近事件含理智结算（如「调查员 理智检定（失败）：损失 4 SAN」）
    san_events = events + [_make_event("调查员 理智检定（失败）：损失 4 SAN", seq=99)]
    chat_service._rule_excerpts_for_context(
        db, module, TurnPlan(turn_kind="combat"), san_events,
    )
    assert captured["q"] == "疯狂 症状 恐惧"


def test_rule_excerpts_gates_and_fail_open(db_factory, monkeypatch):
    """开场（无事件）/ 未挂规则书 / 检索抛错 → 一律 None（fail-open）。
    注意：有玩家事件时兜底 query 总组得出来，「组不出 query」只剩无事件一种情形。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)
    plan = TurnPlan(turn_kind="combat")

    called = {"n": 0}

    def fake_retrieve(*a, **k):
        called["n"] += 1
        return [{"text": "条文", "page": 1, "score": 0.9, "rulebook_id": "x"}]

    monkeypatch.setattr(rulebook_service, "retrieve", fake_retrieve)

    # 开场（无事件）：连 has_rulebook 都不必查
    assert chat_service._rule_excerpts_for_context(db, module, plan, []) is None
    assert chat_service._rule_excerpts_for_context(db, module, None, []) is None

    # 未挂规则书 → None 且不发起检索（即便兜底 query 组得出来）
    monkeypatch.setattr(rulebook_service, "has_rulebook", lambda *a, **k: False)
    assert chat_service._rule_excerpts_for_context(db, module, None, events) is None
    assert chat_service._rule_excerpts_for_context(db, module, plan, events) is None
    assert called["n"] == 0

    # 检索抛错 → fail-open 返回 None
    monkeypatch.setattr(rulebook_service, "has_rulebook", lambda *a, **k: True)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(rulebook_service, "retrieve", boom)
    assert chat_service._rule_excerpts_for_context(db, module, plan, events) is None


def _cap_retrieve(monkeypatch, cap):
    monkeypatch.setattr(rulebook_service, "has_rulebook", lambda *a, **k: True)
    monkeypatch.setattr(
        rulebook_service, "retrieve",
        lambda db_, q, rs, k=3: (cap.__setitem__("q", q),
                                 [{"text": "片段", "page": 1, "score": 1.0, "rulebook_id": "x"}])[1],
    )


def test_rule_excerpts_query_is_situation_specific(db_factory, monkeypatch):
    """query 据**具体技能 + 玩家动作关键词**组合，而非每 turn_kind 一句死词。"""
    db = db_factory()
    module, char, session = _seed(db)
    session_service.add_event(db, session.id, "action", "我贴着阴影潜行过去",
                              actor_id=char.id, actor_name=char.name)
    events = session_service.get_session_events(db, session.id)
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    plan = TurnPlan(turn_kind="investigate", check=CheckPlan(skill="潜行"))
    chat_service._rule_excerpts_for_context(db, module, plan, events)
    assert "潜行" in cap["q"] and "隐匿" in cap["q"]   # 技能名 + 动作关键词都进 query


def test_rule_excerpts_fallback_to_action_when_no_plan(db_factory, monkeypatch):
    """planner 挂了（plan=None）也据玩家动作关键词取规则——不被 planner 失败连累清零。"""
    db = db_factory()
    module, char, session = _seed(db)
    session_service.add_event(db, session.id, "action", "我举枪朝它开火",
                              actor_id=char.id, actor_name=char.name)
    events = session_service.get_session_events(db, session.id)
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    hits = chat_service._rule_excerpts_for_context(db, module, None, events)  # plan=None
    assert hits and "射击" in cap["q"]


def test_rule_query_falls_back_to_player_text(db_factory, monkeypatch):
    """词表/turn_kind 都没命中且 plan=None → 兜底用玩家最近发言原文——有玩家行动就必查。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    hits = chat_service._rule_excerpts_for_context(db, module, None, events)
    assert hits and cap["q"] == "我尝试搬开石板"


def test_planner_rule_query_takes_priority(db_factory, monkeypatch):
    """planner 显式点名的 plan.rule_query 是最高优先级 query——盖过 SAN 情境与词表组合。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)
    san_events = events + [_make_event("调查员 理智检定（失败）：损失 4 SAN", seq=99)]
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    plan = TurnPlan(turn_kind="combat", rule_query="霰弹枪 抵近 伤害")
    hits = chat_service._rule_excerpts_for_context(db, module, plan, san_events)
    assert hits and cap["q"] == "霰弹枪 抵近 伤害"


def test_rule_excerpts_for_planner_from_action(db_factory, monkeypatch):
    """给 planner 的规则片段据玩家动作关键词取（planner 尚无 plan），让裁定更贴规则。"""
    db = db_factory()
    module, char, session = _seed(db)
    session_service.add_event(db, session.id, "action", "我扑上去和它扭打",
                              actor_id=char.id, actor_name=char.name)
    events = session_service.get_session_events(db, session.id)
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    hits = chat_service._rule_excerpts_for_planner(db, module, events)
    assert hits and "擒抱" in cap["q"]


def test_rule_excerpts_for_planner_falls_back_to_player_text(db_factory, monkeypatch):
    """planner 侧词表未命中时同样兜底玩家原话（种子动作「搬开石板」无规则关键词）。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)
    cap = {}
    _cap_retrieve(monkeypatch, cap)
    hits = chat_service._rule_excerpts_for_planner(db, module, events)
    assert hits and cap["q"] == "我尝试搬开石板"


def test_kp_context_injects_rule_excerpts_section(db_factory):
    """rule_excerpts 注入独立「规则要点」小节：单块按 RULE_EXCERPT_MAX_CHARS 截断；
    None（未挂书/开场/plan 缺失）→ 不含小节，行为与现状一致。"""
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    long_text = "规" * (ctx.RULE_EXCERPT_MAX_CHARS + 100)
    system = ctx.build_kp_context(
        session, module, char, events,
        rule_excerpts=[{"text": long_text}, {"text": "短条文"}],
    )[0]["content"]

    assert "规则要点" in system
    assert "裁定时优先遵此执行" in system
    assert long_text not in system  # 单块截断
    assert long_text[:ctx.RULE_EXCERPT_MAX_CHARS] + "…" in system
    assert "短条文" in system

    plain = ctx.build_kp_context(session, module, char, events)[0]["content"]
    assert "规则要点" not in plain
