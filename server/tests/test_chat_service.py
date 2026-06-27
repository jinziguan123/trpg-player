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


def test_stream_and_persist_saves_on_interrupt(db_factory, monkeypatch):
    """流式被取消（如刷新断连）时，已生成内容仍应落库。"""
    monkeypatch.setattr(chat_service, "SessionLocal", db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "KP 刚说到一半"
        yield chat_service._make_chunk("narration", "KP 刚说到一半", actor_name="KP")
        raise asyncio.CancelledError()

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    result = ["", "", []]

    async def run():
        async for _ in chat_service._stream_and_persist(
            db, session_id, None, [], [], result
        ):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "KP 刚说到一半"


def test_stream_and_persist_saves_once_on_success(db_factory, monkeypatch):
    """正常完成时落库一次且不重复（finally 不应二次写入）。"""
    monkeypatch.setattr(chat_service, "SessionLocal", db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "完整的开场叙事"
        yield chat_service._make_chunk("narration", "完整的开场叙事", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    result = ["", "", []]

    asyncio.run(
        _collect(chat_service._stream_and_persist(db, session_id, None, [], [], result))
    )

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "完整的开场叙事"


def test_handle_opening_idempotent(db_factory, monkeypatch):
    """已有开局的会话再次触发 opening 不应重复生成。"""
    monkeypatch.setattr(chat_service, "SessionLocal", db_factory)

    triggered = {"gen": False}

    async def fake_stream_and_persist(db, session_id, kp, messages, npcs, result):
        triggered["gen"] = True
        yield chat_service._make_chunk("narration", "不该发生", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_and_persist", fake_stream_and_persist)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "narration", "已有开局", actor_name="KP")

    chunks = asyncio.run(_collect(chat_service.handle_opening(db, session_id)))

    assert triggered["gen"] is False
    assert any('"type": "done"' in c for c in chunks)
    assert len(_narrations(db_factory, session_id)) == 1
