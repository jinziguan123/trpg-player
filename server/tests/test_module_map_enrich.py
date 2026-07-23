import copy
import json
from types import SimpleNamespace

import pytest

from app.services import module_map_service


class _DB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1


class _LLM:
    def __init__(self, response):
        self.response = response
        self.messages = None
        self.kwargs = None

    async def complete(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _module(scenes, **extra):
    return SimpleNamespace(
        title="测试模组",
        description="公开简介",
        world_setting={"era": "1920s", "region": "北方", "location": "小镇", "tone": "悬疑"},
        scenes=scenes,
        **extra,
    )


def _install_llm(monkeypatch, payload):
    response = payload
    if not isinstance(payload, (str, Exception)):
        response = json.dumps(payload, ensure_ascii=False)
    llm = _LLM(response)
    monkeypatch.setattr(module_map_service, "get_fast_llm", lambda: llm)
    return llm


@pytest.mark.asyncio
async def test_biome_合法值采纳_非法值保留(monkeypatch):
    module = _module([
        {"id": "s1", "kind": "location", "map": {"q": 0, "r": 0, "biome": "plain"}},
        {"id": "s2", "kind": "location", "map": {"q": 2, "r": 0, "biome": "forest"}},
    ], map_nodes=[
        {"id": "s1", "q": 0, "r": 0, "biome": "plain", "scene_id": "s1"},
        {"id": "s2", "q": 2, "r": 0, "biome": "forest", "scene_id": "s2"},
    ])
    _install_llm(monkeypatch, {"scenes": [
        {"id": "s1", "biome": "urban"},
        {"id": "s2", "biome": "volcano"},
    ]})

    result = await module_map_service.enrich_module_map(_DB(), module)

    assert module.scenes[0]["map"]["biome"] == "urban"
    assert module.scenes[1]["map"]["biome"] == "forest"
    map_nodes = {node["scene_id"]: node for node in module.map_nodes if node.get("scene_id")}
    assert map_nodes["s1"]["biome"] == "urban"
    assert result["biomes_updated"] == 1


@pytest.mark.asyncio
async def test_biome_道路地貌可被采纳(monkeypatch):
    module = _module([
        {"id": "roadblock", "kind": "location", "title": "乡村公路哨岗",
         "map": {"q": 0, "r": 0, "biome": "plain"}},
    ], map_nodes=[
        {"id": "roadblock", "q": 0, "r": 0, "biome": "plain", "scene_id": "roadblock"},
    ])
    _install_llm(monkeypatch, {"scenes": [
        {"id": "roadblock", "biome": "road"},
    ]})

    result = await module_map_service.enrich_module_map(_DB(), module)

    assert module.scenes[0]["map"]["biome"] == "road"
    assert module.map_nodes[0]["biome"] == "road"
    assert result["biomes_updated"] == 1


@pytest.mark.asyncio
async def test_独立建筑优先城镇_明确房间才用室内(monkeypatch):
    module = _module([
        {"id": "office", "kind": "location", "title": "公安局办公室", "map": {"q": 0, "r": 0, "biome": "plain"}},
        {"id": "room", "kind": "location", "title": "地下室", "map": {"q": 2, "r": 0, "biome": "plain"}},
    ])
    _install_llm(monkeypatch, {"scenes": [
        {"id": "office", "biome": "interior"},
        {"id": "room", "biome": "interior"},
    ]})

    await module_map_service.enrich_module_map(_DB(), module)

    assert module.scenes[0]["map"]["biome"] == "urban"
    assert module.scenes[1]["map"]["biome"] == "interior"


@pytest.mark.asyncio
async def test_connections_只追加合法目标且不删已有(monkeypatch):
    module = _module([
        {"id": "s1", "kind": "location", "connections": ["s2"],
         "map": {"q": 0, "r": 0, "biome": "plain"}},
        {"id": "s2", "kind": "location", "connections": [],
         "map": {"q": 1, "r": 0, "biome": "plain"}},
        {"id": "s3", "kind": "location", "connections": [],
         "map": {"q": 2, "r": 0, "biome": "plain"}},
    ])
    _install_llm(monkeypatch, {"scenes": [
        {"id": "s1", "add_connections": ["s3", "s2", "s1", "missing"]},
        {"id": "unknown", "add_connections": ["s2"]},
    ]})

    result = await module_map_service.enrich_module_map(_DB(), module)

    assert module.scenes[0]["connections"] == ["s2", "s3"]
    assert result["connections_added"] == 1


@pytest.mark.asyncio
async def test_坐标冲突经修复且章节不处理(monkeypatch):
    module = _module([
        {"id": "s1", "kind": "location", "map": {"q": 0, "r": 0, "biome": "plain"}},
        {"id": "s2", "kind": "location", "map": {"q": 2, "r": 0, "biome": "plain"}},
        {"id": "chapter", "kind": "chapter"},
    ])
    _install_llm(monkeypatch, {"scenes": [
        {"id": "s1", "q": 5, "r": 5, "biome": "forest"},
        {"id": "s2", "q": 5, "r": 5, "biome": "water"},
        {"id": "chapter", "q": 8, "r": 8, "biome": "interior"},
    ]})

    await module_map_service.enrich_module_map(_DB(), module)

    coords = {(scene["map"]["q"], scene["map"]["r"]) for scene in module.scenes[:2]}
    assert len(coords) == 2
    assert module.scenes[0]["map"]["q"] == 5
    assert module.scenes[0]["map"]["r"] == 5
    assert "map" not in module.scenes[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["{bad json", RuntimeError("连接失败")])
async def test_AI失败不落库且场景不变(monkeypatch, response):
    module = _module([
        {"id": "s1", "kind": "location", "map": {"q": 0, "r": 0, "biome": "plain"}},
    ])
    before = copy.deepcopy(module.scenes)
    db = _DB()
    _install_llm(monkeypatch, response)

    with pytest.raises(ValueError):
        await module_map_service.enrich_module_map(db, module)

    assert module.scenes == before
    assert db.commits == 0


@pytest.mark.asyncio
async def test_输入不含秘密且调用不设max_tokens(monkeypatch):
    module = _module(
        [{
            "id": "s1",
            "kind": "location",
            "description": "公开场景" * 80,
            "secrets": ["场景秘密标记"],
            "map": {"q": 0, "r": 0, "biome": "plain"},
        }],
        truth="幕后真相标记",
        clues=[{"description": "线索秘密标记"}],
    )
    llm = _install_llm(monkeypatch, {"scenes": []})

    await module_map_service.enrich_module_map(_DB(), module)

    material = json.loads(llm.messages[1]["content"])
    serialized = llm.messages[1]["content"]
    assert len(material["scenes"][0]["description"]) == 200
    assert "场景秘密标记" not in serialized
    assert "幕后真相标记" not in serialized
    assert "线索秘密标记" not in serialized
    assert llm.kwargs == {"temperature": 0, "response_format": {"type": "json_object"}}
