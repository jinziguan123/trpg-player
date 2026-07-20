"""模组图片失效修复 API。"""

import asyncio
import base64
import io

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import Base, Module  # noqa: F401


def _png_b64() -> str:
    image = Image.new("RGB", (8, 8), (20, 120, 80))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_regenerate_missing_module_image_updates_json(tmp_path, monkeypatch):
    from app.services import image_store, module_image_service

    engine = create_engine(f"sqlite:///{tmp_path / 'module-images.db'}")
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path / "images")

    class PromptLLM:
        async def complete(self, messages, **kwargs):
            assert "提示词工程师" in messages[0]["content"]
            return "old chapel interior"

    class ImageLLM:
        def supports_image_gen(self):
            return True

        async def generate_image(self, prompt, size="1024x1024"):
            assert prompt.startswith("old chapel interior")
            return _png_b64()

    monkeypatch.setattr(module_image_service, "get_fast_llm", lambda: PromptLLM())
    monkeypatch.setattr(module_image_service, "get_llm", lambda: ImageLLM())
    try:
        with TestClient(app) as client:
            db = testing_session()
            module = Module(
                title="m", rule_system="coc",
                scenes=[{"id": "s1", "name": "教堂", "image": "/api/images/missing.jpg"}],
                npcs=[{"id": "n1", "name": "守墓人", "portrait": "/api/images/missing2.jpg"}],
                clues=[{"id": "c1", "name": "日记", "image": "/api/images/missing3.jpg"}],
            )
            db.add(module)
            db.commit()
            module_id = module.id
            db.close()

            response = client.post(
                f"/api/modules/{module_id}/images/regenerate",
                json={"kind": "scene", "item_id": "s1", "field": "image"},
            )
            assert response.status_code == 200, response.text
            url = response.json()["url"]
            assert url.startswith("/api/images/")
            saved = testing_session().get(Module, module_id)
            assert saved.scenes[0]["image"] == url
            assert (tmp_path / "images" / url.rsplit("/", 1)[-1]).is_file()
    finally:
        app.dependency_overrides.clear()


def test_regenerate_reuses_existing_file(tmp_path, monkeypatch):
    from app.services import image_store, module_image_service

    monkeypatch.setattr(image_store, "IMAGES_DIR", tmp_path / "images")
    url = image_store.save_image_b64(_png_b64())
    engine = create_engine(f"sqlite:///{tmp_path / 'module-images.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    module = Module(title="m", rule_system="coc", scenes=[{"id": "s1", "image": url}])
    db.add(module)
    db.commit()

    class NoLLM:
        def supports_image_gen(self):
            raise AssertionError("已有图片文件时不应调用模型")

    monkeypatch.setattr(module_image_service, "get_llm", lambda: NoLLM())
    assert asyncio.run(module_image_service.regenerate_module_image(db, module, "scene", "s1")) == url
