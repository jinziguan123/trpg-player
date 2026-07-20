"""事件序号唯一性与重排回归测试。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Base, EventLog, GameSession, Module
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'event-sequence.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="序号约束测试", rule_system="coc", scenes=[], npcs=[])
    db.add(module)
    db.commit()
    session = GameSession(module_id=module.id, status="active", world_state={})
    db.add(session)
    db.commit()
    return session.id


def test_event_sequence_is_unique_per_session(db_factory):
    db = db_factory()
    sid = _seed(db)
    db.add_all(
        [
            EventLog(session_id=sid, sequence_num=1, event_type="action", content="a"),
            EventLog(session_id=sid, sequence_num=1, event_type="action", content="duplicate"),
        ]
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_same_sequence_is_allowed_for_different_sessions(db_factory):
    db = db_factory()
    first = _seed(db)
    second = _seed(db)
    db.add_all(
        [
            EventLog(session_id=first, sequence_num=1, event_type="action", content="a"),
            EventLog(session_id=second, sequence_num=1, event_type="action", content="b"),
        ]
    )
    db.commit()


def test_reorder_uses_temporary_sequence_range(db_factory):
    db = db_factory()
    sid = _seed(db)
    first = session_service.add_event(db, sid, "narration", "第一段")
    second = session_service.add_event(db, sid, "dice", "骰子")
    third = session_service.add_event(db, sid, "narration", "第二段")

    chat_service._reorder_turn_events(
        db,
        sid,
        [(0, second.id), (1, first.id), (2, third.id)],
        base_seq=0,
    )

    events = session_service.get_session_events(db, sid)
    assert [(e.sequence_num, e.content) for e in events] == [
        (1, "骰子"),
        (2, "第一段"),
        (3, "第二段"),
    ]
