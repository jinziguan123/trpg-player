"""团记导出：事件过滤、分窗、逐窗改写拼装与 fail-open。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401
from app.services import replay_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class _EchoLLM:
    """改写＝在正文前加标记，便于断言「确实经过改写」。"""
    async def complete(self, messages, **kw):
        return "【改写】" + messages[-1]["content"][:20]


class _BoomLLM:
    async def complete(self, messages, **kw):
        raise RuntimeError("boom")


def _seed(db) -> str:
    module = Module(title="鬼屋", rule_system="coc", npcs=[], scenes=[], clues=[])
    char = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, char])
    db.flush()
    session = GameSession(module_id=module.id, player_character_id=char.id, status="active")
    db.add(session)
    db.commit()
    session_service.add_event(db, session.id, "narration", "他们冲进黑暗的走廊。", actor_name="KP")
    session_service.add_event(db, session.id, "dialogue", "小心脚下。", actor_name="向导")
    session_service.add_event(db, session.id, "action", "我举起油灯。", actor_name="调查员")
    # 仅 KP 可见的幕后事件——不应进团记
    session_service.add_event(db, session.id, "system", "幕后：怪物苏醒", actor_name="幕后",
                              metadata={"kind": "backstage"}, visibility=["kp"])
    return session.id


def test_visible_story_events_excludes_kp_only(db_factory):
    db = db_factory()
    sid = _seed(db)
    evs = replay_service._visible_story_events(db, sid)
    kinds = [e.event_type for e in evs]
    assert kinds == ["narration", "dialogue", "action"]  # 幕后 system/kp-only 被排除


def test_window_events_splits_by_budget():
    class _E:
        def __init__(self, c): self.content = c
    long = "字" * 2000  # 远超单窗预算
    evs = [_E("短"), _E(long), _E("短")]
    wins = replay_service._window_events(evs)
    assert len(wins) >= 2  # 超预算断窗
    assert sum(len(w) for w in wins) == 3  # 不丢事件


def test_export_replay_rewrites_and_assembles(db_factory, monkeypatch):
    db = db_factory()
    sid = _seed(db)
    monkeypatch.setattr(replay_service, "get_llm", lambda: _EchoLLM())
    out = asyncio.run(replay_service.export_replay(db, sid, "novel"))
    assert out is not None
    assert out["style"] == "novel"
    assert out["markdown"].startswith("# 鬼屋（小说体团记）")
    assert "【改写】" in out["markdown"]      # 经过改写
    assert "幕后" not in out["markdown"]       # kp-only 不入团记


def test_export_replay_failopen_uses_raw(db_factory, monkeypatch):
    db = db_factory()
    sid = _seed(db)
    monkeypatch.setattr(replay_service, "get_llm", lambda: _BoomLLM())
    out = asyncio.run(replay_service.export_replay(db, sid, "script"))
    assert out is not None
    # 改写失败回退朴素记录：仍含原始事件文本，导出不为空
    assert "冲进黑暗的走廊" in out["markdown"]
    assert out["markdown"].startswith("# 鬼屋（剧本体团记）")


def test_export_replay_no_events_returns_none(db_factory):
    db = db_factory()
    module = Module(title="空", rule_system="coc", npcs=[], scenes=[], clues=[])
    char = Character(name="c", rule_system="coc")
    db.add_all([module, char]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=char.id, status="active")
    db.add(s); db.commit()
    assert asyncio.run(replay_service.export_replay(db, s.id, "novel")) is None
