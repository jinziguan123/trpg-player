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
    monkeypatch.setattr(chat_service, "get_fast_llm", lambda: PromptLLM())
    monkeypatch.setattr(chat_service, "get_llm", lambda: ImageLLM())
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
    monkeypatch.setattr(chat_service, "get_llm", lambda: NoImage())
    asyncio.run(chat_service._illustrate_handout(session.id, ev_b.id, "x", "letter", "y"))
    assert not sent
    assert "image" not in (db_factory().get(EventLog, ev_b.id).metadata_ or {})
