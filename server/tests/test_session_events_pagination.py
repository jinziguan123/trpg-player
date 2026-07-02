"""get_session_events 的默认全量行为回归测试。

守护一个真实 bug：早先默认 limit=100 + 升序 → 会话过百条后只返回「最早 100 条」，
生成上下文看不到最新玩家输入。默认必须返回全量、且末尾是最新事件。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, EventLog, GameSession, Module  # noqa: F401
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_session(db) -> str:
    module = Module(title="截断回归", rule_system="coc", npcs=[], scenes=[])
    db.add(module)
    db.commit()
    session = GameSession(module_id=module.id, status="active", world_state={})
    db.add(session)
    db.commit()
    return session.id


def test_默认返回全量且升序(db_factory):
    db = db_factory()
    sid = _seed_session(db)
    for i in range(150):
        session_service.add_event(db, sid, "action", f"第 {i} 条", actor_name="玩家")

    events = session_service.get_session_events(db, sid)

    assert len(events) == 150, "默认应返回全部事件，不得截断到 100"
    seqs = [e.sequence_num for e in events]
    assert seqs == sorted(seqs), "应按 sequence_num 升序"
    assert events[-1].content == "第 149 条", "末尾必须是最新事件（最新玩家输入不能丢）"


def test_最新玩家输入始终在末尾(db_factory):
    db = db_factory()
    sid = _seed_session(db)
    for i in range(120):
        session_service.add_event(db, sid, "narration", f"旁白 {i}", actor_name="KP")
    session_service.add_event(db, sid, "dialogue", "「这是我最新的一句」", actor_name="玩家")

    events = session_service.get_session_events(db, sid)

    assert events[-1].content == "「这是我最新的一句」"


def test_显式limit仍可分页(db_factory):
    db = db_factory()
    sid = _seed_session(db)
    for i in range(10):
        session_service.add_event(db, sid, "action", f"e{i}", actor_name="玩家")

    first_three = session_service.get_session_events(db, sid, limit=3)

    assert [e.content for e in first_three] == ["e0", "e1", "e2"]
