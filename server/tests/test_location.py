"""按角色位置 / 已知地点 / 地图跟随场景 的单元测试（分头行动 + 大地图前往）。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import map_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loc.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


_MAP_C = {
    "w": 5, "h": 3,
    "tiles": ["#####", "+...#", "#####"],
    "objects": [], "entrances": [{"name": "门", "x": 0, "y": 1}], "npc_pos": [],
}
_SCENES = [
    {"id": "a", "title": "门厅", "connections": ["b", "c"]},
    {"id": "b", "title": "图书馆", "connections": ["a"]},
    {"id": "c", "title": "档案馆", "connections": ["a", "d"], "map": _MAP_C},
    {"id": "d", "title": "隐秘地窖", "connections": ["c"]},  # 起初不与已访问相连 → 不可见
]


def _seed(db):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=_SCENES)
    pc = Character(name="莫妮卡", rule_system="coc")
    ally = Character(name="亨利", rule_system="coc")
    db.add(module); db.add(pc); db.add(ally); db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=pc.id, status="active",
        current_scene_id="a", world_state={"visited_scenes": ["a"]},
    )
    db.add(session); db.commit()
    return session.id, pc.id, ally.id, module.id


class _Ev:
    def __init__(self, etype, content):
        self.event_type, self.content = etype, content


def test_known_locations_are_visited_plus_mentioned(db_factory):
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    session = db.get(GameSession, sid)
    module = db.get(Module, mod_id)
    # 没有任何对话提及时，只有已访问/当前所在（a）可见
    assert session_service.known_scene_ids(module, session, []) == {"a"}
    # 对话里提到「图书馆」「档案馆」→ 解锁 b、c；d（隐秘地窖）未提及仍隐藏
    events = [_Ev("narration", "门上挂着牌子：图书馆、档案馆。")]
    known = session_service.known_scene_ids(module, session, events)
    assert known == {"a", "b", "c"}
    assert "d" not in known
    locs = session_service.list_known_locations(module, session, char_id=pc_id, events=events)
    by_id = {x["id"]: x for x in locs}
    assert by_id["a"]["current"] is True
    assert "d" not in by_id


def test_known_location_unlocked_by_facility_suffix(db_factory):
    """提到设施类型后缀（「疗养院」）即可解锁完整标题含该后缀的地点（如「罗克斯伯里疗养院」）。"""
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    # 给模组加一个「罗克斯伯里疗养院」场景
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [{"id": "san", "title": "罗克斯伯里疗养院", "connections": []}]
    db.commit()
    session = db.get(GameSession, sid)
    events = [_Ev("dialogue", "我们得去那家疗养院问问加布里埃尔。")]
    assert "san" in session_service.known_scene_ids(module, session, events)


def test_set_char_location_moves_player(db_factory):
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    session_service.set_char_location(db, sid, pc_id, "c")
    session = db.get(GameSession, sid)
    assert session.current_scene_id == "c"                 # 主角移动同步 current_scene_id
    assert session_service.get_char_location(session, pc_id) == "c"
    assert "c" in (session.world_state or {}).get("visited_scenes")


def test_scene_map_follows_char_and_filters_party(db_factory):
    db = db_factory()
    sid, pc_id, ally_id, _ = _seed(db)
    # 玩家移到 c（有地图），队友留在 a → c 的地图上只应有玩家、没有队友
    session_service.set_char_location(db, sid, pc_id, "c")
    session = db.get(GameSession, sid)
    out = map_service.current_scene_map(db, session, char_id=pc_id)
    assert out["scene_id"] == "c"
    names = {e["name"] for e in out["entities"]}
    assert "莫妮卡" in names
    assert "亨利" not in names    # 队友在别处，不出现在我的地图上
