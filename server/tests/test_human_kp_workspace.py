"""真人 KP M2-M4：私有工作区、AI 参谋、审核式配图与队友回合标记。"""

import asyncio
import base64
import io

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Character, EventLog, Module
from app.schemas.session import SessionRead
from app.services import chat_service, human_kp_service, image_store, session_service


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'human-kp-workspace.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="真人 KP 工作区", rule_system="coc", rag_status="",
        scenes=[{"id": "s1", "title": "门厅", "description": "枝形吊灯摇晃不止"}],
        npcs=[{"id": "n1", "name": "管家", "description": "穿旧燕尾服", "personality": "警惕"}],
        clues=[{"id": "c1", "name": "铜钥匙", "description": "沾着黑色油污"}],
        handouts=[{"id": "h1", "title": "旧信", "kind": "letter", "content": "午夜前不要开门。"}],
    )
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    ally = Character(name="记者", rule_system="coc", is_player=False)
    db.add_all([module, hero, ally])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": hero.id, "role": "human", "is_primary": True},
            {"character_id": ally.id, "role": "ai"},
        ],
        creator_token="kp-token", kp_mode="human",
    )
    return module, hero, ally, session


def test_workspace_is_private_and_exposes_catalogs(tmp_path):
    db = _db(tmp_path)()
    module, _hero, _ally, session = _seed(db)
    human_kp_service.update_workspace(
        db, session, notes="凶手藏在阁楼", auto_ai_teammates=True,
    )
    payload = human_kp_service.workspace_payload(db, session.id, session, module)
    assert payload["notes"] == "凶手藏在阁楼"
    assert payload["auto_ai_teammates"] is True
    assert payload["catalogs"]["npcs"] == [{"id": "n1", "name": "管家"}]
    assert "kp_state" not in SessionRead.model_validate(session).model_dump()


def test_advisor_draft_and_plan_never_write_public_events(tmp_path, monkeypatch):
    db = _db(tmp_path)()
    module, _hero, _ally, session = _seed(db)

    class DraftLlm:
        async def complete(self, messages, **kwargs):
            return "门厅的灯骤然熄灭。[SET_FLAG: flag=lights_out]"

    class PlanLlm:
        async def complete(self, messages, **kwargs):
            return '{"turn_kind":"investigate","player_intent":"搜查门厅","requires_check":true,"check":{"skill":"侦查","difficulty":"normal"}}'

    monkeypatch.setattr(human_kp_service, "get_llm", lambda: DraftLlm())
    monkeypatch.setattr(human_kp_service, "get_fast_llm", lambda: PlanLlm())
    draft = asyncio.run(human_kp_service.generate_narration_draft(
        db, session.id, session, module, "制造停电的悬念",
    ))
    plan = asyncio.run(human_kp_service.generate_turn_plan(
        db, session.id, session, module, "是否需要侦查",
    ))
    assert draft == "门厅的灯骤然熄灭。"
    assert plan["requires_check"] is True and plan["check"]["skill"] == "侦查"
    assert db.query(EventLog).filter(EventLog.session_id == session.id).count() == 0
    assert not (session.world_state or {}).get("clue_ledger")


def test_image_preview_is_private_until_explicit_publish(tmp_path, monkeypatch):
    db = _db(tmp_path)()
    module, _hero, _ally, session = _seed(db)
    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path / "images")

    class ImageLlm:
        def supports_image_gen(self):
            return True

        async def complete(self, messages, **kwargs):
            return "an abandoned hall with a broken chandelier"

        async def generate_image(self, prompt, size="1024x1024"):
            return _png_b64()

    llm = ImageLlm()
    monkeypatch.setattr(human_kp_service, "get_llm", lambda: llm)
    monkeypatch.setattr(human_kp_service, "get_fast_llm", lambda: llm)
    preview = asyncio.run(human_kp_service.generate_image_preview("废弃门厅", "停电后的门厅"))
    assert preview["url"].startswith("/api/images/")
    assert db.query(EventLog).filter(EventLog.session_id == session.id).count() == 0

    human_kp_service.queue_image_suggestion(
        db, session,
        key="scene:s1:base", title="停电后的门厅", prompt="废弃门厅",
        image_kind="scene", image_item_id="s1", image_field="image",
    )
    event = human_kp_service.publish_image(
        db, session.id, preview["url"], preview["title"], "scene:s1:base",
    )
    assert event.metadata_["icat"] == "custom"
    assert event.metadata_["kp_manual"] is True
    assert event.metadata_["kp_suggestion_key"] == "scene:s1:base"
    db.refresh(module)
    db.refresh(session)
    assert module.scenes[0]["image"] == preview["url"]
    assert (session.kp_state or {}).get("image_suggestions") == []


def test_human_kp_automatic_images_only_enter_private_review_queue(tmp_path, monkeypatch):
    db = _db(tmp_path)()
    module, hero, ally, session = _seed(db)

    def unexpected_llm():
        raise AssertionError("真人 KP 自动入口不应直接调用生图模型")

    monkeypatch.setattr(chat_service, "get_llm", unexpected_llm)
    assert chat_service._maybe_scene_illustration(db, session.id, module, "s1") == []
    chat_service._maybe_clue_illustration(db, session.id, module, "c1")
    assert chat_service._maybe_encounter_illustration(
        db, session.id, module, [dict(module.npcs[0])],
    ) == []
    dialogue = session_service.add_event(
        db, session.id, "dialogue", "晚上好。", actor_id="n1", actor_name="管家",
    )
    chat_service._attach_npc_portrait(db, session.id, module, dialogue)
    asyncio.run(chat_service._exec_handout(
        db, session.id, session, module, "h1", hero, [ally],
    ))

    db.refresh(session)
    suggestions = (session.kp_state or {}).get("image_suggestions") or []
    assert {item["key"].split(":", 1)[0] for item in suggestions} == {
        "scene", "clue", "encounter", "npc-portrait", "handout",
    }
    events = db.query(EventLog).filter(EventLog.session_id == session.id).all()
    assert not any((event.metadata_ or {}).get("kind") == "illustration" for event in events)
    db.refresh(dialogue)
    assert "portrait" not in (dialogue.metadata_ or {})


def test_ai_team_turn_marker_is_idempotent(tmp_path):
    db = _db(tmp_path)()
    _module, hero, _ally, session = _seed(db)
    session_service.add_event(
        db, session.id, "action", "我检查门锁", actor_id=hero.id, actor_name=hero.name,
        metadata={"pending_turn": True},
    )
    assert not human_kp_service.has_unprocessed_player_turn(db, session.id, session)
    session_service.commit_turn(db, session.id)
    assert human_kp_service.has_unprocessed_player_turn(db, session.id, session)
    marker = human_kp_service.current_player_turn_marker(db, session.id)
    human_kp_service.update_workspace(db, session)
    state = dict(session.kp_state or {})
    state["last_ai_team_turn_seq"] = marker
    session.kp_state = state
    db.commit()
    assert not human_kp_service.has_unprocessed_player_turn(db, session.id, session)


def test_workspace_api_requires_kp_token(tmp_path):
    db_factory = _db(tmp_path)

    def override_get_db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    db = db_factory()
    _module, _hero, _ally, session = _seed(db)
    sid = session.id
    db.close()
    try:
        client = TestClient(app)
        denied = client.get(
            f"/api/sessions/{sid}/kp/workspace",
            headers={"X-Player-Token": "other-token"},
        )
        assert denied.status_code == 403
        allowed = client.get(
            f"/api/sessions/{sid}/kp/workspace",
            headers={"X-Player-Token": "kp-token"},
        )
        assert allowed.status_code == 200
        assert "notes" in allowed.json()
    finally:
        app.dependency_overrides.clear()
