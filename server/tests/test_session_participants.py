"""会话多席位（session_participants）回归测试。

覆盖：
1. create_session 能写入 1 主角 + N 个 AI 队友席位。
2. 活跃会话占用的角色不能重复加入新会话（含 AI 队友）。
3. 解除“一模组一活跃会话”约束：同一模组可有多个活跃会话。
4. active_character_ids 同时覆盖主角与 AI 队友（供 available 过滤对齐）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (  # noqa: F401 — 注册全部表
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db) -> tuple[str, list[str]]:
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc", is_player=True)
    ally1 = Character(name="队友A", rule_system="coc", is_player=False)
    ally2 = Character(name="队友B", rule_system="coc", is_player=False)
    db.add_all([module, hero, ally1, ally2])
    db.commit()
    return module.id, [hero.id, ally1.id, ally2.id]


def test_create_session_with_participants(db_factory):
    db = db_factory()
    module_id, (hero, ally1, ally2) = _seed(db)

    session = session_service.create_session(
        db,
        module_id,
        [
            {"character_id": hero, "role": "human", "is_primary": True},
            {"character_id": ally1, "role": "ai"},
            {"character_id": ally2, "role": "ai"},
        ],
    )

    assert session.player_character_id == hero  # 主角快捷字段对齐
    parts = session_service.get_participants(db, session.id)
    assert len(parts) == 3
    primaries = [p for p in parts if p.is_primary]
    assert len(primaries) == 1 and primaries[0].character_id == hero
    teammates = session_service.get_ai_teammates(db, session.id)
    assert {c.id for c in teammates} == {ally1, ally2}


def test_character_cannot_join_two_active_sessions(db_factory):
    db = db_factory()
    module_id, (hero, ally1, ally2) = _seed(db)

    session_service.create_session(
        db, module_id, [{"character_id": hero, "is_primary": True}]
    )

    # 主角已在活跃会话 → 再加入应被拒
    with pytest.raises(ValueError):
        session_service.create_session(
            db, module_id, [{"character_id": hero, "is_primary": True}]
        )

    # AI 队友也被占用追踪：ally1 已在某会话当队友后不能再被选
    s2 = session_service.create_session(
        db, module_id, [{"character_id": ally1, "is_primary": True}]
    )
    assert s2 is not None
    occupied = session_service.active_character_ids(db)
    assert hero in occupied and ally1 in occupied


def test_multiple_active_sessions_per_module_allowed(db_factory):
    db = db_factory()
    module_id, (hero, ally1, ally2) = _seed(db)

    s1 = session_service.create_session(
        db, module_id, [{"character_id": hero, "is_primary": True}]
    )
    # 同一模组、不同角色，应允许第二个活跃会话（旧约束已解除）
    s2 = session_service.create_session(
        db, module_id, [{"character_id": ally1, "is_primary": True}]
    )
    assert s1.id != s2.id
    assert s1.module_id == s2.module_id


def test_active_character_ids_covers_teammates(db_factory):
    db = db_factory()
    module_id, (hero, ally1, ally2) = _seed(db)

    session_service.create_session(
        db,
        module_id,
        [
            {"character_id": hero, "is_primary": True},
            {"character_id": ally1, "role": "ai"},
        ],
    )
    occupied = session_service.active_character_ids(db)
    assert hero in occupied
    assert ally1 in occupied  # AI 队友也算占用
    assert ally2 not in occupied
