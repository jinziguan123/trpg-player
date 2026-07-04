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


def test_resolve_context_budget_scales_with_window():
    """组装预算按窗口自适应：≤64K 回落下限、中窗口线性放大、超大窗口封顶。"""
    from app.ai.context import (
        CONTEXT_BUDGET_CEIL,
        CONTEXT_BUDGET_WINDOW_FRACTION,
        CONTEXT_TOKEN_BUDGET,
        resolve_context_budget,
    )
    # 64K（DeepSeek）：0.6×64K < 下限 → 回落下限，行为与放宽前完全一致
    assert resolve_context_budget(65_536) == CONTEXT_TOKEN_BUDGET
    # 200K（Claude）：线性放大，未触顶
    assert resolve_context_budget(200_000) == int(200_000 * CONTEXT_BUDGET_WINDOW_FRACTION)
    # 1M（Gemini/gpt-4.1）：0.6×1M 远超上限 → 封顶
    assert resolve_context_budget(1_000_000) == CONTEXT_BUDGET_CEIL
    # 放大后始终在 [下限, 上限] 内，且随窗口单调不减
    assert CONTEXT_TOKEN_BUDGET <= resolve_context_budget(256_000) <= CONTEXT_BUDGET_CEIL
    assert resolve_context_budget(256_000) >= resolve_context_budget(128_000)


def test_resolve_context_budget_invalid_window_falls_back():
    from app.ai.context import CONTEXT_TOKEN_BUDGET, resolve_context_budget
    assert resolve_context_budget(0) == CONTEXT_TOKEN_BUDGET
    assert resolve_context_budget(-1) == CONTEXT_TOKEN_BUDGET


def test_estimate_prefers_measured_usage(db_factory):
    """world_state.turn_usage 存在时，占用真值用服务端实测 prompt_tokens，ratio 随之。"""
    from app.ai.context import RESERVE_FOR_OUTPUT
    db = db_factory()
    sid = _seed(db)
    s = db.get(GameSession, sid)
    ws = dict(s.world_state)
    ws["turn_usage"] = {"prompt_tokens": 12345, "completion_tokens": 600, "total_tokens": 12945, "at_seq": 6}
    s.world_state = ws
    db.commit()
    r = context_estimate.estimate_session_context(db, sid)
    assert r["source"] == "measured"
    assert r["measured_input_tokens"] == 12345
    expected = round((12345 + RESERVE_FOR_OUTPUT) / r["context_window"], 4)
    assert abs(r["usage_ratio"] - expected) < 1e-6


def test_estimate_falls_back_to_estimate_without_usage(db_factory):
    db = db_factory()
    sid = _seed(db)   # 无 turn_usage
    r = context_estimate.estimate_session_context(db, sid)
    assert r["source"] == "estimated"
    assert r["measured_input_tokens"] is None
    assert r["input_tokens"] > 0


def test_record_turn_usage_persists_and_failopen(db_factory):
    from app.services import chat_service
    db = db_factory()
    sid = _seed(db)
    s = db.get(GameSession, sid)
    events = session_service.get_session_events(db, sid, limit=0)

    class _LLM:
        last_usage = {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000}
    chat_service._record_turn_usage(db, s, _LLM(), events)
    assert (db.get(GameSession, sid).world_state or {})["turn_usage"]["prompt_tokens"] == 900

    class _LLM2:
        last_usage = None   # 不支持 usage → 不覆盖、不报错
    chat_service._record_turn_usage(db, s, _LLM2(), events)
    assert (db.get(GameSession, sid).world_state or {})["turn_usage"]["prompt_tokens"] == 900
