"""chat_service 流式持久化与 opening 幂等的回归测试。

用临时文件 SQLite + monkeypatch 模拟断流，不依赖真实 LLM。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_session(db) -> str:
    module = Module(title="测试模组", rule_system="coc", npcs=[])
    char = Character(name="测试角色", rule_system="coc")
    db.add(module)
    db.add(char)
    db.flush()
    session = GameSession(
        module_id=module.id,
        player_character_id=char.id,
        status="active",
    )
    db.add(session)
    db.commit()
    return session.id


async def _collect(agen) -> list:
    return [chunk async for chunk in agen]


def _narrations(db_factory, session_id) -> list:
    return [
        e
        for e in session_service.get_session_events(db_factory(), session_id)
        if e.event_type == "narration"
    ]


def _patch_runtime(monkeypatch, db_factory):
    """把 chat_service 的运行期依赖换成测试可控的桩。"""
    import app.database as database
    from app.services.room_hub import room_hub

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_service, "get_llm", lambda: None)
    monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)


def test_generation_saves_on_interrupt(db_factory, monkeypatch):
    """流式被取消（硬取消生成 task）时，已生成内容仍应落库。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "KP 刚说到一半"
        yield chat_service._make_chunk("narration", "KP 刚说到一半", actor_name="KP")
        raise asyncio.CancelledError()

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "dialogue", "我环顾四周", actor_name="玩家")

    # run_chat_generation 内部吞掉 CancelledError，但叙事应已在 finally 落库
    asyncio.run(chat_service.run_chat_generation(session_id))

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "KP 刚说到一半"


def test_generation_saves_once_on_success(db_factory, monkeypatch):
    """正常完成时落库一次且不重复。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "完整的开场叙事"
        yield chat_service._make_chunk("narration", "完整的开场叙事", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    asyncio.run(chat_service.run_opening_generation(session_id))

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "完整的开场叙事"


def test_opening_idempotent(db_factory, monkeypatch):
    """已有事件的会话再次触发 opening 不应重复生成。"""
    _patch_runtime(monkeypatch, db_factory)

    triggered = {"gen": False}

    async def fake_stream(kp, messages, result, npcs=None):
        triggered["gen"] = True
        result[0] = "不该发生"
        yield chat_service._make_chunk("narration", "不该发生", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "narration", "已有开局", actor_name="KP")

    asyncio.run(chat_service.run_opening_generation(session_id))

    assert triggered["gen"] is False
    assert len(_narrations(db_factory, session_id)) == 1
