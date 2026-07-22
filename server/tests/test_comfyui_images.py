"""ComfyUI 文生图对接：工作流注入 / Provider 委托 / 图片存取 / 手书配图管线。不打真网。"""

import asyncio
import base64
import io
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import comfyui
from app.ai.provider import LLMProvider
from app.models import Base, Character, EventLog, GameSession, Module  # noqa: F401
from app.services import session_service


# ── 工作流注入 ──────────────────────────────────────────────


USER_WF = json.dumps({
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "PLACEHOLDER_POSITIVE", "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "PLACEHOLDER_NEGATIVE", "clip": ["1", 1]}},
    "5": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 25}},
})


def test_build_workflow_injects_placeholders_and_randomizes_seed():
    wf = comfyui.build_workflow(USER_WF, "an old letter", "blurry")
    assert wf["2"]["inputs"]["text"] == "an old letter"
    assert wf["3"]["inputs"]["text"] == "blurry"
    assert wf["5"]["inputs"]["seed"] != 0          # seed 随机化（否则同提示词恒出同图）

    # 负面占位留空 → 用默认负面；坏 JSON → None（fail-open）
    wf2 = comfyui.build_workflow(USER_WF, "x", "")
    assert wf2["3"]["inputs"]["text"] == comfyui.DEFAULT_NEGATIVE
    assert comfyui.build_workflow("{不是json", "x") is None

    # 未配置工作流 → 内置默认模板，且不污染模板本体
    wf3 = comfyui.build_workflow("", "prompt-a")
    assert wf3["2"]["inputs"]["text"] == "prompt-a"
    assert comfyui.DEFAULT_WORKFLOW["2"]["inputs"]["text"] == comfyui.POSITIVE_PLACEHOLDER


def test_client_fails_open_without_base_url():
    client = comfyui.ComfyUIClient("", USER_WF)
    assert asyncio.run(client.generate("x")) is None


# ── Provider 委托 ────────────────────────────────────────────


class _FakeComfy:
    async def generate(self, prompt, negative=""):
        return "FAKE_B64"


class _Bare(LLMProvider):
    """最小 Provider：验证任何协议挂上 ComfyUI 都获得生图能力。"""
    async def complete(self, *a, **k):
        return ""

    async def stream(self, *a, **k):
        yield ""


def test_provider_gains_image_gen_via_comfyui():
    p = _Bare()
    assert p.supports_image_gen() is False
    assert asyncio.run(p.generate_image("x")) is None
    p.set_comfyui(_FakeComfy())
    assert p.supports_image_gen() is True
    assert asyncio.run(p.generate_image("x")) == "FAKE_B64"


def test_openai_provider_prefers_comfyui(monkeypatch):
    from app.ai.providers.openai_compat import OpenAICompatProvider

    prov = OpenAICompatProvider(model="m", api_key="k", image_model="dall-e-3")
    prov.set_comfyui(_FakeComfy())
    # 挂了 ComfyUI：不打 OpenAI Images 端点，直接走内网出图
    assert asyncio.run(prov.generate_image("x")) == "FAKE_B64"


def test_factory_attaches_comfyui_for_any_protocol(monkeypatch):
    from app.api import ai_settings
    from app.ai import llm_factory

    profile = ai_settings.AIProfile(
        name="主", protocol="anthropic", model_name="claude-x", api_key="k",
        image_backend="comfyui", comfyui_base_url="http://172.30.18.236:8188",
    )
    prov = llm_factory.provider_from_profile(profile)
    assert prov.supports_image_gen() is True       # Anthropic 协议也获得生图能力
    assert prov._comfyui.base_url == "http://172.30.18.236:8188"


# ── 图片存取 ────────────────────────────────────────────────


def _png_b64() -> str:
    from PIL import Image

    im = Image.new("RGB", (8, 8), (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_image_store_and_endpoint(tmp_path, monkeypatch):
    """落盘转 JPEG + 端点白名单：正常取图 200；穿越/未知一律 404。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import image_store
    import app.api.images as images_api

    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(images_api, "IMAGES_DIR", tmp_path)

    url = image_store.save_image_b64(_png_b64())
    assert url and url.startswith("/api/images/") and url.endswith(".jpg")

    c = TestClient(app)
    ok = c.get(url)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert c.get("/api/images/nonexistent0000000000000000000000.jpg").status_code == 404
    assert c.get("/api/images/..%2F..%2Ftrpg.db").status_code in (404, 422)
    assert image_store.save_image_b64("not-base64!!") is None   # 坏输入弃图不抛


# ── 手书配图管线 ─────────────────────────────────────────────


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'img.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_illustrate_handout_patches_event_and_broadcasts(db_factory, monkeypatch, tmp_path):
    """配图完成：事件 metadata 补 image、广播 event_patch；提示词/生图失败则静默无痕。"""
    import app.database as database
    from app.services import chat_service, image_store

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path)

    db = db_factory()
    module = Module(title="m", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()
    ev = session_service.add_event(
        db, session.id, "system", "亲爱的哈维……", actor_name="系统",
        metadata={"kind": "handout", "title": "遗书"},
    )

    class PromptLLM:
        async def complete(self, messages, **kw):
            assert "提示词工程师" in messages[0]["content"]
            return "aged letter on wooden desk, 1920s, candlelight"

    class ImageLLM:
        def supports_image_gen(self):
            return True

        async def generate_image(self, prompt, size="1024x1024"):
            assert "aged letter" in prompt
            return _png_b64()

    sent: list[str] = []
    monkeypatch.setattr(chat_service.illustration_service, "get_fast_llm", lambda: PromptLLM())
    monkeypatch.setattr(chat_service.illustration_service, "get_llm", lambda: ImageLLM())
    monkeypatch.setattr(chat_service.room_hub, "broadcast", lambda sid, chunk: sent.append(chunk))

    asyncio.run(chat_service._illustrate_handout(session.id, ev.id, "遗书", "letter", ev.content))

    ev2 = db_factory().get(EventLog, ev.id)
    url = (ev2.metadata_ or {}).get("image")
    assert url and url.startswith("/api/images/")
    patch_chunks = [c for c in sent if '"event_patch"' in c]
    assert len(patch_chunks) == 1 and ev.id in patch_chunks[0] and url in patch_chunks[0]

    # 生图失败：无 metadata 变化、无广播（静默降级）
    class NoImage(ImageLLM):
        async def generate_image(self, prompt, size="1024x1024"):
            return None

    sent.clear()
    ev_b = session_service.add_event(db, session.id, "system", "第二封", actor_name="系统",
                                     metadata={"kind": "handout"})
    monkeypatch.setattr(chat_service.illustration_service, "get_llm", lambda: NoImage())
    asyncio.run(chat_service._illustrate_handout(session.id, ev_b.id, "x", "letter", "y"))
    assert not sent
    assert "image" not in (db_factory().get(EventLog, ev_b.id).metadata_ or {})


# ── 场景/NPC/线索配图接入点 ──────────────────────────────────


class _CountingImageLLM:
    """计数生图桩：断言「缓存命中时不再烧卡」的唯一真源是 calls 长度。"""

    def __init__(self, calls: list):
        self.calls = calls

    def supports_image_gen(self):
        return True

    async def generate_image(self, prompt, size="1024x1024"):
        self.calls.append(prompt)
        return _png_b64()


class _StubPromptLLM:
    def __init__(self, prompt: str):
        self.prompt = prompt

    async def complete(self, messages, **kw):
        assert "提示词工程师" in messages[0]["content"]
        return self.prompt


def _wire_image_stubs(monkeypatch, db_factory, tmp_path, prompt: str):
    """接线通用桩：独立 DB 会话工厂 / 图片目录 / 快模型提示词 / 计数生图 / 广播收集。"""
    import app.database as database
    from app.services import chat_service, image_store

    calls: list = []
    sent: list = []
    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(
        chat_service.illustration_service, "get_fast_llm", lambda: _StubPromptLLM(prompt),
    )
    monkeypatch.setattr(
        chat_service.illustration_service, "get_llm", lambda: _CountingImageLLM(calls),
    )
    monkeypatch.setattr(chat_service.room_hub, "broadcast", lambda sid, chunk: sent.append(chunk))
    return calls, sent


async def _drain_bg_tasks():
    """等待 _spawn_illustration 起的后台配图任务全部收尾。"""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def test_scene_illustration_first_entry_generates_and_caches(db_factory, monkeypatch, tmp_path):
    """场景首入：落卡→后台生图→回写 scenes[].image→patch 事件；同会话防重；新会话缓存秒出。"""
    from app.services import chat_service

    calls, sent = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "abandoned chapel, fog")
    db = db_factory()
    module = Module(
        title="m", rule_system="coc", npcs=[], world_setting={"era": "1920s"},
        scenes=[{"id": "s1", "title": "废弃教堂", "description": "塌了一半的教堂",
                 "danger": "uneasy", "atmosphere": "霉味与烛泪"}],
    )
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()

    async def first_entry():
        chunks = chat_service._maybe_scene_illustration(db, session.id, module, "s1")
        assert len(chunks) == 1 and "抵达" in chunks[0] and "废弃教堂" in chunks[0]
        # 同会话再次进入：scene_cards 防重 → 不再落卡、不再生图
        assert chat_service._maybe_scene_illustration(db, session.id, module, "s1") == []
        await _drain_bg_tasks()

    asyncio.run(first_entry())

    db2 = db_factory()
    cards = [
        e for e in session_service.get_session_events(db2, session.id)
        if (e.metadata_ or {}).get("kind") == "illustration"
    ]
    assert len(cards) == 1
    meta = cards[0].metadata_
    assert meta["icat"] == "scene" and meta["title"] == "废弃教堂"
    assert meta["image_kind"] == "scene" and meta["image_item_id"] == "s1"
    url = meta.get("image")
    assert url and url.startswith("/api/images/")
    assert any('"event_patch"' in c and url in c for c in sent)
    assert db2.get(Module, module.id).scenes[0]["image"] == url    # 缓存已回写模组
    assert len(calls) == 1

    # 新会话（缓存已在）：卡片 metadata 直接带图秒出，不再起生图任务
    hero2 = Character(name="主角乙", rule_system="coc")
    db2.add(hero2); db2.commit()
    session2 = GameSession(module_id=module.id, player_character_id=hero2.id, status="active")
    db2.add(session2); db2.commit()
    m2 = db2.get(Module, module.id)

    async def second_session():
        chunks = chat_service._maybe_scene_illustration(db2, session2.id, m2, "s1")
        assert len(chunks) == 1 and url in chunks[0]
        await _drain_bg_tasks()

    asyncio.run(second_session())
    assert len(calls) == 1                                          # 未再生图


def test_scene_illustration_discards_missing_cached_file(db_factory, monkeypatch, tmp_path):
    """模组仍有旧 URL 但文件已被删除时，首入必须重新生成并回写新 URL。"""
    from app.services import chat_service

    calls, _ = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "rebuilt chapel")
    db = db_factory()
    module = Module(
        title="m", rule_system="coc", npcs=[],
        scenes=[{"id": "s1", "title": "教堂", "image": "/api/images/deleted.jpg"}],
    )
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()

    async def enter():
        chunks = chat_service._maybe_scene_illustration(db, session.id, module, "s1")
        assert len(chunks) == 1 and "/api/images/deleted.jpg" not in chunks[0]
        await _drain_bg_tasks()

    asyncio.run(enter())
    saved = db_factory().get(Module, module.id)
    assert saved.scenes[0]["image"].startswith("/api/images/")
    assert saved.scenes[0]["image"] != "/api/images/deleted.jpg"
    assert len(calls) == 1


def test_scene_visual_state_creates_one_variant_card_and_reuses_it(db_factory, monkeypatch, tmp_path):
    """视觉字段随 flag 改变时生成状态图；同一状态再次进入不重复出卡。"""
    from app.services import chat_service

    calls, _sent = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "flooded chapel")
    db = db_factory()
    module = Module(
        title="m", rule_system="coc", npcs=[],
        scenes=[{
            "id": "s1", "title": "教堂", "description": "空旷的教堂",
            "atmosphere": "安静", "states": [{
                "when": ["flooded"], "visual_variant": "flooded", "atmosphere": "齐腰黑水",
            }],
        }],
    )
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id, status="active",
        current_scene_id="s1", world_state={"flags": {}},
    )
    db.add(session); db.commit()

    async def run():
        assert len(chat_service._maybe_scene_illustration(db, session.id, module, "s1")) == 1
        await _drain_bg_tasks()
        db.refresh(session)
        assert len(chat_service._exec_flag(db, session.id, session, "flooded", True)) == 2
        db.refresh(session)
        m2 = db.get(Module, module.id)
        await _drain_bg_tasks()
        assert chat_service._maybe_scene_illustration(db, session.id, m2, "s1") == []

    asyncio.run(run())
    fresh = db_factory()
    cards = [
        e for e in session_service.get_session_events(fresh, session.id)
        if (e.metadata_ or {}).get("icat") == "scene"
    ]
    assert len(cards) == 2
    assert {e.metadata_.get("visual_state_key") for e in cards} == {"base", "flooded"}
    variants = fresh.get(Module, module.id).scenes[0].get("image_variants") or {}
    assert variants.get("flooded", "").startswith("/api/images/")
    assert len(calls) == 2


def test_missing_scene_file_repairs_existing_card_without_duplicate(db_factory, monkeypatch, tmp_path):
    """当前会话已有场景卡但文件被删时，修复原卡而不是新增第二张。"""
    from app.services import chat_service, image_store

    calls, _sent = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "repaired chapel")
    db = db_factory()
    module = Module(title="m", rule_system="coc", npcs=[], scenes=[{"id": "s1", "title": "教堂"}])
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()

    async def run():
        chat_service._maybe_scene_illustration(db, session.id, module, "s1")
        await _drain_bg_tasks()
        url = db_factory().get(Module, module.id).scenes[0]["image"]
        (image_store.IMAGES_DIR / url.rsplit("/", 1)[-1]).unlink()
        db.expire_all()
        m2 = db.get(Module, module.id)
        assert chat_service._maybe_scene_illustration(db, session.id, m2, "s1") == []
        await _drain_bg_tasks()

    asyncio.run(run())
    fresh = db_factory()
    cards = [e for e in session_service.get_session_events(fresh, session.id) if (e.metadata_ or {}).get("icat") == "scene"]
    assert len(cards) == 1
    assert fresh.get(Module, module.id).scenes[0]["image"] != "/api/images/deleted.jpg"
    assert len(calls) == 2


def test_npc_portrait_generates_then_hits_cache(db_factory, monkeypatch, tmp_path):
    """NPC 立绘：首次对话生成+回写 npcs[].portrait+patch；再次对话缓存直挂 metadata、不再生图。"""
    from app.services import chat_service

    calls, sent = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "old keeper, bust portrait")
    monkeypatch.setattr(chat_service.illustration_service, "_PORTRAIT_INFLIGHT", set())
    db = db_factory()
    module = Module(
        title="m", rule_system="coc", scenes=[],
        npcs=[{"id": "npc_1", "name": "老守墓人", "description": "佝偻老者", "personality": "寡言"}],
    )
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()

    ev1 = session_service.add_event(db, session.id, "dialogue", "别在夜里来。", actor_name="老守墓人")

    async def first_dialogue():
        chat_service._attach_npc_portrait(db, session.id, module, ev1)
        await _drain_bg_tasks()

    asyncio.run(first_dialogue())

    db2 = db_factory()
    url = (db2.get(EventLog, ev1.id).metadata_ or {}).get("portrait")
    assert url and url.startswith("/api/images/")
    assert db2.get(Module, module.id).npcs[0]["portrait"] == url    # 缓存已回写模组
    assert any('"event_patch"' in c and '"portrait"' in c and url in c for c in sent)
    assert len(calls) == 1
    assert chat_service._PORTRAIT_INFLIGHT == set()                 # 防重标记已清

    # 再次对话：缓存命中 → metadata 直挂立绘并广播增量，不再生图（无后台任务，直接同步走完）
    m2 = db2.get(Module, module.id)
    ev2 = session_service.add_event(db2, session.id, "dialogue", "……走吧。", actor_name="老守墓人")
    chat_service._attach_npc_portrait(db2, session.id, m2, ev2)
    assert (db_factory().get(EventLog, ev2.id).metadata_ or {}).get("portrait") == url
    assert len(calls) == 1


def test_clue_illustration_card_only_on_first_reveal(db_factory, monkeypatch, tmp_path):
    """线索卡：planner 首次把线索记入台账时落一张发现卡（生图+回写 clues[].image）；
    同一线索后续再被裁定揭示不重复出卡。"""
    from app.ai import turn_planner
    from app.services import chat_service

    calls, sent = _wire_image_stubs(monkeypatch, db_factory, tmp_path, "bloodstained diary close-up")
    db = db_factory()
    module = Module(
        title="m", rule_system="coc", scenes=[], npcs=[],
        clues=[{"id": "clue_1", "name": "血字日记", "description": "以血写就的日记残页"}],
    )
    hero = Character(name="主角", rule_system="coc")
    db.add_all([module, hero]); db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session); db.commit()

    plan = turn_planner.TurnPlan(clue_policy=turn_planner.CluePolicy(
        candidate_clue_ids=["clue_1"], reveal_level="direct",
    ))

    async def reveal_twice():
        chat_service._record_clue_ledger_from_plan(db, session, plan, [], hero, None, module=module)
        # 第二轮同线索再揭示：已在台账 → 不再出第二张卡
        chat_service._record_clue_ledger_from_plan(db, session, plan, [], hero, None, module=module)
        await _drain_bg_tasks()

    asyncio.run(reveal_twice())

    db2 = db_factory()
    cards = [
        e for e in session_service.get_session_events(db2, session.id)
        if (e.metadata_ or {}).get("kind") == "illustration"
    ]
    assert len(cards) == 1
    meta = cards[0].metadata_
    assert meta["icat"] == "clue" and meta["title"] == "血字日记"
    assert meta["image_kind"] == "clue" and meta["image_item_id"] == "clue_1"
    assert "发现线索" in cards[0].content
    url = meta.get("image")
    assert url and url.startswith("/api/images/")
    assert db2.get(Module, module.id).clues[0]["image"] == url      # 缓存已回写模组
    assert len(calls) == 1
    # 台账本身照常记账（配图是增强件，不改变世界记忆行为）
    assert "clue_1" in (db2.get(GameSession, session.id).world_state or {}).get("clue_ledger", {})
