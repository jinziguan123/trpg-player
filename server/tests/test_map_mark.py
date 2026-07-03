"""叙事锚定 [MAP_MARK]：落图、幂等、锚点解析失败不落、合并进运行时地图、旁白内联剔除。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import chat_service, map_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mk.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


_BASE_MAP = {
    "w": 6, "h": 3, "tiles": ["######", "+....#", "######"],
    "entrances": [{"name": "门", "x": 0, "y": 1}],
    "objects": [{"name": "书桌", "x": 3, "y": 1, "kind": "furniture"}],
    "npc_pos": [],
}


def _seed(db):
    scene = {"id": "s1", "name": "前厅", "map": _BASE_MAP, "states": []}
    mod = Module(title="t", rule_system="coc", scenes=[scene], npcs=[], clues=[])
    hero = Character(name="阿强", rule_system="coc", is_player=True)
    db.add_all([mod, hero]); db.commit()
    sess = GameSession(module_id=mod.id, player_character_id=hero.id, status="active",
                       current_scene_id="s1", world_state={"flags": {}})
    db.add(sess); db.commit()
    return sess


def test_map_mark_lands_near_anchor_and_merges(db_factory):
    db = db_factory()
    sess = _seed(db)
    map_service.apply_map_mark(db, sess, "半掩的门", "书桌", "feature")
    marks = (sess.world_state.get("map_marks") or {}).get("s1")
    assert marks and marks[0]["name"] == "半掩的门" and marks[0]["kind"] == "feature"
    # 落在书桌(3,1)近旁的空地板
    assert abs(marks[0]["x"] - 3) + abs(marks[0]["y"] - 1) <= 2
    # 运行时地图把标记并入 objects（前端零改动即渲染）
    out = map_service.current_scene_map(db, sess)
    names = [o["name"] for o in out["floors"][0]["map"]["objects"]]
    assert "半掩的门" in names and "书桌" in names


def test_map_mark_idempotent_same_name_updates(db_factory):
    db = db_factory()
    sess = _seed(db)
    map_service.apply_map_mark(db, sess, "血迹", "书桌", "feature")
    map_service.apply_map_mark(db, sess, "血迹", "门", "item")
    marks = (sess.world_state.get("map_marks") or {}).get("s1")
    assert len(marks) == 1  # 同名更新而非重复添加
    assert marks[0]["kind"] == "item"


def test_map_mark_unresolvable_near_is_noop(db_factory):
    """near 解析不了 → 不落（错位标记比没有标记更误导）。"""
    db = db_factory()
    sess = _seed(db)
    map_service.apply_map_mark(db, sess, "裂缝", "不存在的锚点", "feature")
    assert "map_marks" not in (sess.world_state or {})


def test_map_mark_same_name_as_module_object_not_duplicated(db_factory):
    """标记与模组自带物体同名时，合并层跳过、不重影。"""
    db = db_factory()
    sess = _seed(db)
    map_service.apply_map_mark(db, sess, "书桌", "门", "furniture")
    out = map_service.current_scene_map(db, sess)
    names = [o["name"] for o in out["floors"][0]["map"]["objects"]]
    assert names.count("书桌") == 1


def test_map_mark_tag_stripped_from_narration():
    """[MAP_MARK] 是内部标记：与 [MOVE] 一样从旁白就地剔除、不打断行文。"""
    raw = "走廊尽头，一扇门虚掩着。[MAP_MARK: name=半掩的门, near=书桌, kind=feature]门缝里透出微光。"
    result = ["", "", [], [], []]

    async def go():
        async def stream():
            for ch in raw:
                yield ch
        async for _ in chat_service._filter_narration_stream(stream(), result, npcs=[]):
            pass
    asyncio.run(go())
    assert "MAP_MARK" not in result[0]
    assert "走廊尽头，一扇门虚掩着。门缝里透出微光。" == result[0]


def test_exec_map_mark_tolerates_bad_input(db_factory):
    """执行入口 fail-open：缺 name 不执行、异常不抛。"""
    db = db_factory()
    sess = _seed(db)
    chat_service._exec_map_mark(db, sess, {"near": "书桌"})   # 缺 name → no-op
    assert "map_marks" not in (sess.world_state or {})
    chat_service._exec_map_mark(db, sess, {"name": "裂缝", "near": "书桌", "kind": "怪值"})
    marks = (sess.world_state.get("map_marks") or {}).get("s1")
    assert marks and marks[0]["kind"] == "feature"  # 非法 kind 回落 feature
