"""OOC（场外，小括号）消息：拆分、不入 KP 上下文、不触发生成。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.database import get_db
from app.main import app
from app.models import (  # noqa: F401
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)
from app.services import chat_service, session_service


def test_split_ooc():
    assert chat_service.split_ooc("我推开门") == ("我推开门", "")
    assert chat_service.split_ooc("（等我去倒杯水）") == ("", "等我去倒杯水")
    assert chat_service.split_ooc("(brb)") == ("", "brb")
    # 混合：括号外是正式行动，括号内是场外
    ic, ooc = chat_service.split_ooc("我搜查桌子（这线索是不是和上关有关？）")
    assert ic == "我搜查桌子"
    assert ooc == "这线索是不是和上关有关？"


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ooc.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = session_service.create_session(
        db, module.id, [{"character_id": hero.id, "is_primary": True}]
    )
    return module, hero, session


def test_ooc_event_excluded_from_kp_context(db_factory):
    db = db_factory()
    module, hero, session = _seed(db)
    session_service.add_event(db, session.id, "dialogue", "我推开门", actor_id=hero.id, actor_name=hero.name)
    session_service.add_event(db, session.id, "ooc", "等下我接个电话", actor_id=hero.id, actor_name=hero.name)
    events = session_service.get_session_events(db, session.id)

    messages = ctx.build_kp_context(session, module, hero, events)
    joined = "\n".join(m["content"] for m in messages)
    assert "我推开门" in joined
    assert "等下我接个电话" not in joined  # OOC 不进入 KP 上下文


def test_ooc_endpoint_persists_without_generation(db_factory):
    engine_session = db_factory

    def override_get_db():
        d = engine_session()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        db = engine_session()
        module, hero, session = _seed(db)
        sid = session.id
        db.close()

        client = TestClient(app)
        r = client.post(f"/api/sessions/{sid}/ooc", json={"content": "（先暂停一下）"})
        assert r.status_code == 200, r.text

        db2 = engine_session()
        events = session_service.get_session_events(db2, sid)
        ooc = [e for e in events if e.event_type == "ooc"]
        assert len(ooc) == 1
        assert ooc[0].content == "先暂停一下"  # 去掉括号
        db2.close()
    finally:
        app.dependency_overrides.clear()


def test_ooc_endpoint_allows_reserved_human_kp_without_character(db_factory):
    engine_session = db_factory

    def override_get_db():
        d = engine_session()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        db = engine_session()
        module = Module(title="真人 KP 模组", rule_system="coc", npcs=[], scenes=[])
        db.add(module)
        db.commit()
        session = session_service.create_session(
            db,
            module.id,
            [{"character_id": None, "role": "human", "is_primary": True}],
            creator_token="kp-token",
            kp_mode="human",
        )
        sid = session.id
        db.close()

        client = TestClient(app)
        response = client.post(
            f"/api/sessions/{sid}/ooc",
            json={"content": "（先确认一下规则）"},
            headers={"X-Player-Token": "kp-token"},
        )
        assert response.status_code == 200, response.text
        typing = client.post(
            f"/api/sessions/{sid}/typing",
            headers={"X-Player-Token": "kp-token"},
        )
        assert typing.status_code == 200, typing.text

        db2 = engine_session()
        event = session_service.get_session_events(db2, sid)[-1]
        assert event.actor_id is None and event.actor_name == "真人 KP"
        db2.close()
    finally:
        app.dependency_overrides.clear()


def test_ooc_endpoint_allows_reserved_player_without_character(db_factory):
    engine_session = db_factory

    def override_get_db():
        d = engine_session()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        db = engine_session()
        module = Module(title="访客大厅模组", rule_system="coc", npcs=[], scenes=[])
        hero = Character(name="房主角色", rule_system="coc", is_player=True)
        db.add_all([module, hero])
        db.commit()
        session = session_service.create_session(
            db,
            module.id,
            [
                {"character_id": hero.id, "role": "human", "is_primary": True},
                {"character_id": None, "role": "human"},
            ],
            creator_token="host-token",
        )
        session_service.join_session(db, session.id, "guest-token")
        sid = session.id
        db.close()

        client = TestClient(app)
        response = client.post(
            f"/api/sessions/{sid}/ooc",
            json={"content": "（我想生成一名记者）"},
            headers={"X-Player-Token": "guest-token"},
        )
        assert response.status_code == 200, response.text

        db2 = engine_session()
        event = session_service.get_session_events(db2, sid)[-1]
        assert event.actor_id is None and event.actor_name == "玩家"
        db2.close()
    finally:
        app.dependency_overrides.clear()
