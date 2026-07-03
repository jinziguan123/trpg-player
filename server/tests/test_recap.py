"""战报 recap：结构化生成（含脏数据兜底）+ 落库到 world_state.recaps。"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import recap as recap_ai
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401
from app.services import recap_service, session_service


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, **kw):
        return self.payload


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


_GOOD = json.dumps({
    "title": "疗养院惊魂",
    "key_decisions": ["决定潜入地下室", ""],   # 空串应被过滤
    "clues_resolved": ["找到了钥匙"],
    "clues_unresolved": ["册子背面的注释含义"],
    "highlights": [{"seq": 42, "quote": "恶魔终将被自己的武器击败"}, {"quote": "无seq也收"}, {"bad": 1}],
    "casualties": [],
}, ensure_ascii=False)


def test_generate_recap_parses_and_sanitizes():
    r = asyncio.run(recap_ai.generate_recap(
        _FakeLLM(_GOOD), prev_summary="前情", events=[object()],
    ))
    assert r["title"] == "疗养院惊魂"
    assert r["key_decisions"] == ["决定潜入地下室"]  # 空串被过滤
    assert r["clues_unresolved"] == ["册子背面的注释含义"]
    assert r["highlights"] == [
        {"seq": 42, "quote": "恶魔终将被自己的武器击败"},
        {"seq": None, "quote": "无seq也收"},
    ]
    assert r["casualties"] == []


def test_generate_recap_empty_title_returns_none():
    r = asyncio.run(recap_ai.generate_recap(
        _FakeLLM(json.dumps({"title": ""})), prev_summary="", events=[object()],
    ))
    assert r is None


def test_generate_recap_bad_json_returns_none():
    r = asyncio.run(recap_ai.generate_recap(
        _FakeLLM("这不是 JSON"), prev_summary="", events=[object()],
    ))
    assert r is None


def test_generate_recap_no_events_returns_none():
    r = asyncio.run(recap_ai.generate_recap(_FakeLLM(_GOOD), prev_summary="", events=[]))
    assert r is None


def _seed(db) -> str:
    module = Module(title="鬼屋", rule_system="coc", npcs=[], scenes=[], clues=[])
    char = Character(
        name="调查员", rule_system="coc", is_player=True,
        system_data={"hitPoints": {"current": 3, "max": 13}, "sanity": {"current": 20, "max": 99}},
    )
    db.add_all([module, char])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=char.id, status="active",
        world_state={"story_summary": "一路凶险"},
    )
    db.add(session)
    db.commit()
    session_service.add_event(db, session.id, "narration", "他们冲进黑暗。", actor_name="KP")
    return session.id


def test_generate_and_store_recap_persists(db_factory, monkeypatch):
    db = db_factory()
    sid = _seed(db)
    monkeypatch.setattr(recap_service, "get_llm", lambda: _FakeLLM(_GOOD))
    entry = asyncio.run(recap_service.generate_and_store_recap(db, sid))
    assert entry is not None
    assert entry["title"] == "疗养院惊魂"
    assert "up_to_seq" in entry and "generated_at" in entry
    # 已落库到 world_state.recaps
    stored = recap_service.list_recaps(db_factory(), sid) if False else recap_service.list_recaps(db, sid)
    assert len(stored) == 1 and stored[0]["title"] == "疗养院惊魂"


def test_generate_and_store_recap_failopen(db_factory, monkeypatch):
    db = db_factory()
    sid = _seed(db)
    monkeypatch.setattr(recap_service, "get_llm", lambda: _FakeLLM("坏数据"))
    entry = asyncio.run(recap_service.generate_and_store_recap(db, sid))
    assert entry is None
    assert recap_service.list_recaps(db, sid) == []  # 失败不落库
