"""上下文占用预估：分项 token 估算、窗口占比与健康度，以及模型窗口解析。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.ai_settings import AIProfile, resolve_context_window
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401
from app.services import context_estimate, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db) -> str:
    module = Module(title="鬼屋", rule_system="coc", npcs=[], scenes=[])
    char = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, char])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=char.id, status="active",
        world_state={"story_summary": "前情摘要若干", "story_summary_seq": 3},
    )
    db.add(session)
    db.commit()
    for i in range(6):
        session_service.add_event(
            db, session.id, "narration", f"第{i}段旁白，描述了房间里的种种细节。",
            actor_name="KP",
        )
    return session.id


def test_estimate_structure_and_sums(db_factory):
    db = db_factory()
    sid = _seed(db)
    r = context_estimate.estimate_session_context(db, sid)
    assert r is not None
    b = r["breakdown"]
    assert b["system"] + b["summary"] + b["history"] == r["input_tokens"]
    assert r["input_tokens"] > 0
    assert r["context_window"] > 0
    assert r["output_reserve"] > 0
    # 事件计数自洽：已摘要（seq<=游标）+ 逐条候选 = 总数
    assert r["events"]["summarized"] + r["events"]["verbatim_candidates"] == r["events"]["total"]
    assert r["events"]["total"] == 6
    assert r["events"]["summarized"] == 3  # seq 1..3 <= 游标 3


def test_estimate_status_thresholds():
    assert context_estimate._status(0.3) == "ok"
    assert context_estimate._status(0.85) == "warn"
    assert context_estimate._status(0.99) == "critical"


def test_estimate_missing_session_returns_none(db_factory):
    db = db_factory()
    assert context_estimate.estimate_session_context(db, "nope") is None


def test_resolve_context_window_explicit_wins():
    p = AIProfile(model_name="deepseek-v4-flash", context_window=12345)
    assert resolve_context_window(p) == 12345


def test_resolve_context_window_by_model_name():
    assert resolve_context_window(AIProfile(model_name="claude-opus-4-8")) == 200_000
    assert resolve_context_window(AIProfile(model_name="deepseek-v4-flash")) == 65_536
    assert resolve_context_window(AIProfile(model_name="gpt-4o-mini")) == 128_000


def test_resolve_context_window_unknown_falls_back():
    assert resolve_context_window(AIProfile(model_name="某国产小模型")) == 65_536
    assert resolve_context_window(None) == 65_536
