"""场景地图生成服务测试：提示词约束、机器校验、逐场景生成并落库（用 fake LLM）。"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module  # noqa: F401
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


def test_prompt_lists_asset_catalog_and_cleans_ids(monkeypatch):
    """素材库目录进提示；生成结果里库中不存在的 asset_id 被剔除、有效的保留。"""
    scene = {"id": "s1", "name": "甲", "description": "房间", "connections": []}
    assets = [{"id": "a1", "name": "石棺", "kind": "furniture"}, {"id": "fl", "name": "地板", "kind": "floor"}]
    prompt = map_service.build_map_prompt(scene, [], ["石棺"], {}, assets)
    assert "a1｜石棺" in prompt
    assert "地板" not in prompt.split("素材库可用素材")[-1]  # 地形类不进可选目录

    out_map = {**GOOD_MAP, "objects": [
        {"name": "石棺", "x": 2, "y": 1, "kind": "furniture", "asset_id": "a1"},   # 有效
        {"name": "怪箱", "x": 2, "y": 1, "kind": "furniture", "asset_id": "ghost"},  # 库里没有
    ]}

    class FakeLLM3:
        async def complete(self, messages, **kw):
            assert "a1｜石棺" in messages[0]["content"]
            return json.dumps(out_map, ensure_ascii=False)
    monkeypatch.setattr(map_service, "get_llm", lambda: FakeLLM3())
    import asyncio
    m = asyncio.run(map_service.generate_scene_map(scene, [], ["石棺"], {}, assets))
    ids = [o.get("asset_id") for o in m["objects"]]
    assert "a1" in ids and "ghost" not in ids  # 有效保留、编造剔除


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


def test_current_scene_map_resolves_and_places_player(db_factory):
    """运行时：返回当前场景地图 + 玩家落在入口；flag 变体带 map 时用变体地图。"""
    db = db_factory()
    base_map = {"w": 5, "h": 3, "tiles": ["#####", "+...#", "#####"], "entrances": [{"name": "门", "x": 0, "y": 1}]}
    alt_map = {"w": 3, "h": 3, "tiles": ["###", "#.#", "###"]}
    scene = {"id": "s1", "name": "甲", "map": base_map,
             "states": [{"when": ["wall_broken"], "map": alt_map}]}
    mod = Module(title="t", rule_system="coc", scenes=[scene], npcs=[], clues=[])
    hero = Character(name="阿强", rule_system="coc", is_player=True)
    db.add_all([mod, hero]); db.commit()
    sess = GameSession(module_id=mod.id, player_character_id=hero.id, status="active",
                       current_scene_id="s1", world_state={"flags": {}})
    db.add(sess); db.commit()

    out = map_service.current_scene_map(db, sess)
    assert out["map"]["tiles"] == base_map["tiles"]
    assert out["entities"] == [{"name": "阿强", "x": 0, "y": 1, "kind": "player"}]  # 落在入口门

    # 置 flag 后用变体地图（打破墙壁→新地图）
    sess.world_state = {"flags": {"wall_broken": True}}
    db.commit()
    out2 = map_service.current_scene_map(db, sess)
    assert out2["map"]["tiles"] == alt_map["tiles"]


def test_generate_variant_map(monkeypatch):
    """据基础图 + 变化说明产出同尺寸变体图（含校验），用 fake LLM。"""
    import asyncio
    variant = {"w": 5, "h": 3, "tiles": ["#####", "+...+", "#####"],
               "objects": [], "entrances": [{"name": "新口", "x": 4, "y": 1}], "npc_pos": []}

    class FakeLLM2:
        async def complete(self, messages, **kw):
            assert "西墙被打破" in messages[0]["content"]  # hint 进了提示
            return json.dumps(variant, ensure_ascii=False)
    monkeypatch.setattr(map_service, "get_llm", lambda: FakeLLM2())
    out = asyncio.run(map_service.generate_variant_map(GOOD_MAP, "西墙被打破，露出新出口"))
    assert out["tiles"] == variant["tiles"]
    assert "_issues" not in out  # 校验通过
