"""场景地图生成服务测试：提示词约束、机器校验、逐场景生成并落库（用 fake LLM）。"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Module  # noqa: F401
from app.services import map_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


GOOD_MAP = {
    "w": 5, "h": 3,
    "tiles": ["#####", "+...#", "#####"],
    "objects": [{"name": "书桌", "x": 2, "y": 1, "kind": "furniture"}],
    "entrances": [{"name": "门", "x": 0, "y": 1, "to": "s2"}],
    "npc_pos": [],
    "notes": "小房间",
}


class _FakeLLM:
    async def complete(self, messages, **kw):
        return json.dumps(GOOD_MAP, ensure_ascii=False)


def test_validate_map_accepts_good():
    assert map_service.validate_map(GOOD_MAP) == []


def test_validate_map_flags_bad_coords():
    bad = {**GOOD_MAP, "objects": [{"name": "雕像", "x": 0, "y": 0, "kind": "feature"}]}  # 落在墙#上
    issues = map_service.validate_map(bad)
    assert any("雕像" in s for s in issues)

    bad2 = {**GOOD_MAP, "tiles": ["#####", "+...#"]}  # 行数≠h
    assert any("行数" in s for s in map_service.validate_map(bad2))


def test_build_map_prompt_maps_connection_ids():
    scene = {"id": "s1", "name": "前室", "description": "堆满宝物", "connections": ["s2"]}
    prompt = map_service.build_map_prompt(scene, npcs=["守卫"], clues=["石板"], scene_names={"s2": "耳室"})
    assert "前室" in prompt and "堆满宝物" in prompt
    assert "耳室（s2）" in prompt          # 连接名→id 映射进提示
    assert "守卫" in prompt and "石板" in prompt
    assert "不剧透" in prompt or "中性名" in prompt


def test_generate_maps_persists_and_skips(db_factory, monkeypatch):
    monkeypatch.setattr(map_service, "get_llm", lambda: _FakeLLM())
    db = db_factory()
    mod = Module(
        title="地图测试", rule_system="coc",
        scenes=[{"id": "s1", "name": "甲", "description": "一个房间", "connections": ["s2"]},
                {"id": "s2", "name": "乙", "description": "另一个房间", "map": {"w": 1, "h": 1, "tiles": ["."]}}],
        npcs=[], clues=[],
    )
    db.add(mod); db.commit()

    import asyncio
    out = asyncio.run(map_service.generate_maps_for_module(db, mod.id))
    by_id = {s["id"]: s for s in out.scenes}
    assert by_id["s1"]["map"]["tiles"] == GOOD_MAP["tiles"]   # 无 map 的场景已生成
    assert by_id["s2"]["map"] == {"w": 1, "h": 1, "tiles": ["."]}  # 已有 map 的场景被跳过

    # force=True 时连已有的也重生成
    out2 = asyncio.run(map_service.generate_maps_for_module(db, mod.id, force=True))
    by_id2 = {s["id"]: s for s in out2.scenes}
    assert by_id2["s2"]["map"]["tiles"] == GOOD_MAP["tiles"]
