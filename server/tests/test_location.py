"""按角色位置 / 已知地点 的单元测试（分头行动 + 大地图前往）。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.services import session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loc.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


_SCENES = [
    {"id": "a", "title": "门厅", "connections": ["b", "c"]},
    {"id": "b", "title": "图书馆", "connections": ["a"]},
    {"id": "c", "title": "档案馆", "connections": ["a", "d"]},
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


def test_locations_connections_only_within_known(db_factory):
    """connections 只回已知集合内的邻居——未知地点（d）绝不经边泄露。"""
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    session = db.get(GameSession, sid)
    module = db.get(Module, mod_id)
    events = [_Ev("narration", "门上挂着牌子：图书馆、档案馆。")]
    locs = session_service.list_known_locations(module, session, char_id=pc_id, events=events)
    by_id = {x["id"]: x for x in locs}
    # c 与 a、d 相连，但 d 未知 → 边里只剩 a
    assert by_id["c"]["connections"] == ["a"]
    assert set(by_id["a"]["connections"]) == {"b", "c"}


def test_locations_chapter_scenes_hidden_unless_current(db_factory):
    """kind=chapter 的叙事章节不上调查板；但当前正身处其中时仍显示（玩家得能找到自己）。"""
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [{"id": "ch", "title": "委托与准备", "kind": "chapter", "connections": []}]
    db.commit()
    session = db.get(GameSession, sid)
    ws = dict(session.world_state); ws["visited_scenes"] = ["a", "ch"]
    session.world_state = ws; db.commit()
    # 已访问但非当前 → 不显示
    locs = session_service.list_known_locations(module, session, char_id=pc_id, events=[])
    assert "ch" not in {x["id"] for x in locs}
    # 正身处其中 → 显示
    session.current_scene_id = "ch"; db.commit()
    locs = session_service.list_known_locations(module, session, char_id=pc_id, events=[])
    assert "ch" in {x["id"] for x in locs}


def test_locations_party_distribution(db_factory):
    """char_names 给定时，按 party_locations（缺省回落主场景）归并各地点在场成员。"""
    db = db_factory()
    sid, pc_id, ally_id, mod_id = _seed(db)
    session = db.get(GameSession, sid)
    module = db.get(Module, mod_id)
    ws = dict(session.world_state)
    ws["visited_scenes"] = ["a", "c"]
    ws["party_locations"] = {ally_id: "c"}   # 亨利分头去了档案馆；莫妮卡缺省在主场景 a
    session.world_state = ws; db.commit()
    locs = session_service.list_known_locations(
        module, session, char_id=pc_id, events=[],
        char_names={pc_id: "莫妮卡", ally_id: "亨利"},
    )
    by_id = {x["id"]: x for x in locs}
    assert by_id["a"]["party"] == ["莫妮卡"]
    assert by_id["c"]["party"] == ["亨利"]


def test_locations_clues_only_discovered_and_located(db_factory):
    """调查板红线：只有 clue_ledger 里**已发现**、且模组定义带 location 的线索挂到地点上。"""
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.clues = [
        {"id": "clue_a", "name": "带血的信", "location": "a"},
        {"id": "clue_b", "name": "地窖钥匙", "location": "d"},   # d 未知 → 其线索不该出现
        {"id": "clue_c", "name": "未发现的真相", "location": "a"},  # 未进台账 → 不上板
    ]
    db.commit()
    session = db.get(GameSession, sid)
    ws = dict(session.world_state)
    ws["clue_ledger"] = {
        "clue_a": {"status": "known", "discovered_by": [pc_id]},
        "clue_b": {"status": "partial", "discovered_by": [pc_id]},
    }
    session.world_state = ws; db.commit()
    locs = session_service.list_known_locations(module, session, char_id=pc_id, events=[])
    by_id = {x["id"]: x for x in locs}
    assert by_id["a"]["clues"] == [{"id": "clue_a", "name": "带血的信", "status": "known"}]
    assert "d" not in by_id  # 未知地点整体不显示，其线索自然也不泄露


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


def test_house_unlocked_by_generic_suffix_mention(db_factory):
    """提到较宽泛的「房子」也能解锁标题以「房子」结尾的地点（如「科比特的老房子」）。"""
    db = db_factory()
    sid, _, _, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [{"id": "house", "title": "科比特的老房子", "connections": []}]
    db.commit()
    session = db.get(GameSession, sid)
    events = [_Ev("narration", "他把钥匙推过来：「这栋房子就一直空着。」")]
    assert "house" in session_service.known_scene_ids(module, session, events)


def test_derive_keywords_strips_state_modifier_suffix():
    """核心修复：『沉思礼拜堂废墟』要派生出核心名『沉思礼拜堂』『礼拜堂』『沉思』——
    这样说『沉思礼拜堂』（不带废墟）也能解锁；修饰词『废墟』本身不作关键词。"""
    kw = session_service.derive_scene_keywords("沉思礼拜堂废墟")
    assert "沉思礼拜堂废墟" in kw and "沉思礼拜堂" in kw and "礼拜堂" in kw and "沉思" in kw
    assert "废墟" not in kw
    # 向后兼容：无修饰词的旧例不变
    assert session_service.derive_scene_keywords("罗克斯伯里疗养院") == {
        "罗克斯伯里疗养院", "疗养院", "罗克斯伯里",
    }


def test_chapel_ruins_unlocked_by_core_name(db_factory):
    """回归用户报告：说『沉思礼拜堂』（不带废墟、老模组无存储 keywords）也能解锁该地点。"""
    db = db_factory()
    sid, _, _, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [{"id": "chapel", "title": "沉思礼拜堂废墟", "connections": []}]
    db.commit()
    session = db.get(GameSession, sid)
    events = [_Ev("dialogue", "我们已经知道沉思礼拜堂的地址了，去那儿看看。")]
    assert "chapel" in session_service.known_scene_ids(module, session, events)


def test_stored_keywords_unlock_by_any_including_address(db_factory):
    """解析时存储的 keywords（含地址/俗称）：玩家提到任一即解锁——与派生取并集。"""
    db = db_factory()
    sid, _, _, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [{
        "id": "chapel", "title": "沉思礼拜堂废墟", "connections": [],
        "keywords": ["沉思礼拜堂", "断头谷路13号", "老教堂"],
    }]
    db.commit()
    session = db.get(GameSession, sid)
    # 提到门牌地址即可解锁（纯派生做不到，靠存储 keywords）
    events = [_Ev("action", "我们照卷宗上的地址前往断头谷路13号。")]
    assert "chapel" in session_service.known_scene_ids(module, session, events)


def test_scene_change_moves_player_and_colocated_party(db_factory):
    """[SCENE_CHANGE] 明确移动：主角切场景，同处的队友一同前往；目的地变已访问、可见。"""
    import asyncio
    from app.services import chat_service

    db = db_factory()
    sid, pc_id, ally_id, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    game_session = db.get(GameSession, sid)
    player = db.get(Character, pc_id)
    ally = db.get(Character, ally_id)

    async def run():
        return [c async for c in chat_service._process_commands(
            db, sid, "他们走进档案馆。[SCENE_CHANGE: scene_id=c]",
            module, player, game_session, None, teammates=[ally],
        )]

    asyncio.run(run())
    session = db.get(GameSession, sid)
    assert session_service.get_char_location(session, pc_id) == "c"     # 主角移动
    assert session_service.get_char_location(session, ally_id) == "c"   # 同处队友一同前往
    assert "c" in (session.world_state or {}).get("visited_scenes")     # 目的地变已访问
    assert "c" in session_service.known_scene_ids(module, session, [])  # 已访问即可见


def test_find_scene_path_graph(db_factory):
    """连通图：邻居/多跳 BFS/无向闭包；不连通 → None；无图或孤点保守放行。"""
    db = db_factory()
    _sid, _pc, _ally, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    assert session_service.scene_neighbors(module, "a") == ["b", "c"]
    assert session_service.find_scene_path(module, "a", "a") == ["a"]
    assert session_service.find_scene_path(module, "a", "d") == ["a", "c", "d"]       # 多跳
    assert session_service.find_scene_path(module, "d", "b") == ["d", "c", "a", "b"]  # 单向填写按双向走

    # 不连通的孤岛群 → None；完全无边的孤点 → 保守放行（作者没建边，无拓扑可循）
    m2 = Module(title="M2", rule_system="coc", npcs=[], scenes=_SCENES + [
        {"id": "e", "title": "梦境", "connections": ["f"]},
        {"id": "f", "title": "深渊", "connections": ["e"]},
        {"id": "z", "title": "无边孤点"},
    ])
    db.add(m2); db.commit()
    assert session_service.find_scene_path(m2, "a", "e") is None
    assert session_service.find_scene_path(m2, "a", "z") == ["a", "z"]

    # 整个模组没建图 → 平凡路径（旧行为，不把旧模组走死）
    m3 = Module(title="M3", rule_system="coc", npcs=[], scenes=[{"id": "x"}, {"id": "y"}])
    db.add(m3); db.commit()
    assert session_service.find_scene_path(m3, "x", "y") == ["x", "y"]


def test_scene_change_rejects_disconnected(db_factory):
    """确定性连通校验：KP 的 scene_change 指到不连通场景 → 拒绝落位并给出可读原因。"""
    import asyncio
    from app.services import chat_service

    db = db_factory()
    sid, pc_id, _ally_id, mod_id = _seed(db)
    module = db.get(Module, mod_id)
    module.scenes = _SCENES + [
        {"id": "e", "title": "月面", "connections": ["f"]},
        {"id": "f", "title": "环形山", "connections": ["e"]},
    ]
    db.commit()
    game_session = db.get(GameSession, sid)
    player = db.get(Character, pc_id)

    chunks, moved, note = asyncio.run(chat_service._exec_scene_change(
        db, sid, game_session, module, "e", player, None,
    ))
    assert moved is None and not chunks
    assert "不连通" in note                                   # 拒绝原因回灌 KP
    assert session_service.get_char_location(db.get(GameSession, sid), pc_id) == "a"  # 没被搬走


def test_set_char_location_moves_player(db_factory):
    db = db_factory()
    sid, pc_id, _, mod_id = _seed(db)
    session_service.set_char_location(db, sid, pc_id, "c")
    session = db.get(GameSession, sid)
    assert session.current_scene_id == "c"                 # 主角移动同步 current_scene_id
    assert session_service.get_char_location(session, pc_id) == "c"
    assert "c" in (session.world_state or {}).get("visited_scenes")


