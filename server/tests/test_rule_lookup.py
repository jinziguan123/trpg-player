"""[RULE_LOOKUP] 按需规则书查阅的集成回归（不依赖真实 LLM / 嵌入）。

覆盖：上下文按是否挂载规则书广告该能力、_process_commands 把 RULE_LOOKUP
当终止性指令路由并短路其余、_handle_rule_lookup 检索→回灌→续写落库与降级。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
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
