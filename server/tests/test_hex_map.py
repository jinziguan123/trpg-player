"""六边形沙盘 P-Hex-1 单测：坐标数学、落位修复的确定性/幂等、KP 空间语义注入（不调 LLM）。"""

from app.ai.context import build_kp_context
from app.models import Character, GameSession, Module
from app.services import hex_map, session_service


# ── axial 坐标数学 ──


class TestAxialMath:
    def test_距离(self):
        assert hex_map.axial_distance((0, 0), (0, 0)) == 0
        assert hex_map.axial_distance((0, 0), (1, 0)) == 1
        assert hex_map.axial_distance((0, 0), (2, -1)) == 2
        assert hex_map.axial_distance((0, 0), (1, -2)) == 2   # 正北两格

    def test_八方位词(self):
        assert hex_map.direction_word((0, 0), (1, -2)) == "北"
        assert hex_map.direction_word((0, 0), (1, -1)) == "东北"
        assert hex_map.direction_word((0, 0), (1, 0)) == "东"
        assert hex_map.direction_word((0, 0), (0, 1)) == "东南"
        assert hex_map.direction_word((0, 0), (-1, 2)) == "南"
        assert hex_map.direction_word((0, 0), (-1, 1)) == "西南"
        assert hex_map.direction_word((0, 0), (-1, 0)) == "西"
        assert hex_map.direction_word((0, 0), (0, -1)) == "西北"
        assert hex_map.direction_word((0, 0), (0, 0)) == ""   # 同格

    def test_远近词分档(self):
        assert hex_map.distance_word(0) == "同处"
        assert hex_map.distance_word(1) == "紧邻"
        assert hex_map.distance_word(3) == "不远"
        assert hex_map.distance_word(6) == "有些路程"
        assert hex_map.distance_word(7) == "相当远"


# ── 落位修复（确定性、幂等、只补洞不推翻）──


def _chain(n=3, with_map=None):
    """a-b-c… 链式连通的场景组；with_map 给指定下标预置坐标。"""
    ids = [f"s{i}" for i in range(n)]
    scenes = []
    for i, sid in enumerate(ids):
        s = {"id": sid, "title": f"场景{i}", "kind": "location",
             "connections": [ids[i + 1]] if i + 1 < n else []}
        if with_map and i in with_map:
            s["map"] = with_map[i]
        scenes.append(s)
    return scenes


class TestEnsureSceneMaps:
    def test_空白模组全量落位且相连就近(self):
        scenes = _chain(4)
        assert hex_map.ensure_scene_maps(scenes) is True
        coords = [hex_map.scene_coord(s) for s in scenes]
        assert all(c is not None for c in coords)
        assert len(set(coords)) == 4                      # 不重叠
        for a, b in zip(coords, coords[1:]):
            assert hex_map.axial_distance(a, b) <= 2      # 相连的就近落位

    def test_幂等且合法提议保留(self):
        scenes = _chain(3, with_map={0: {"q": 5, "r": -3, "biome": "urban"}})
        hex_map.ensure_scene_maps(scenes)
        assert hex_map.scene_coord(scenes[0]) == (5, -3)  # LLM 提议不被推翻
        assert scenes[0]["map"]["biome"] == "urban"
        snapshot = [dict(s["map"]) for s in scenes]
        assert hex_map.ensure_scene_maps(scenes) is False  # 第二次无改动
        assert [dict(s["map"]) for s in scenes] == snapshot

    def test_坐标冲突后者重排(self):
        scenes = _chain(2, with_map={0: {"q": 0, "r": 0, "biome": "plain"},
                                     1: {"q": 0, "r": 0, "biome": "plain"}})
        hex_map.ensure_scene_maps(scenes)
        assert hex_map.scene_coord(scenes[0]) == (0, 0)   # 列表序先到先得
        assert hex_map.scene_coord(scenes[1]) != (0, 0)

    def test_确定性同输入同输出(self):
        a, b = _chain(5), _chain(5)
        hex_map.ensure_scene_maps(a)
        hex_map.ensure_scene_maps(b)
        assert [s["map"] for s in a] == [s["map"] for s in b]

    def test_chapter不落位且清除误给(self):
        scenes = [
            {"id": "ch", "title": "委托与准备", "kind": "chapter",
             "map": {"q": 9, "r": 9, "biome": "plain"}},
            {"id": "s0", "title": "老宅", "kind": "location", "connections": []},
        ]
        hex_map.ensure_scene_maps(scenes)
        assert "map" not in scenes[0]                     # chapter 的误给被清掉
        assert hex_map.scene_coord(scenes[1]) is not None

    def test_biome归一与非法值兜底(self):
        scenes = _chain(2, with_map={0: {"q": 0, "r": 0, "biome": "URBAN"},
                                     1: {"q": 1, "r": 0, "biome": "太空"}})
        hex_map.ensure_scene_maps(scenes)
        assert scenes[0]["map"]["biome"] == "urban"
        assert scenes[1]["map"]["biome"] == "plain"

    def test_非整数坐标视为缺失(self):
        scenes = _chain(1, with_map={0: {"q": "北", "r": 0, "biome": "plain"}})
        hex_map.ensure_scene_maps(scenes)
        assert hex_map.scene_coord(scenes[0]) is not None


class TestNeighborLabel:
    def test_有坐标出方位标签(self):
        cur = {"map": {"q": 0, "r": 0, "biome": "urban"}}
        nb = {"map": {"q": 1, "r": -2, "biome": "forest"}}
        assert hex_map.neighbor_label(cur, nb) == "北・不远"

    def test_任一侧无坐标返回None(self):
        cur = {"map": {"q": 0, "r": 0, "biome": "urban"}}
        assert hex_map.neighbor_label(cur, {}) is None
        assert hex_map.neighbor_label({}, cur) is None

    def test_地貌中文名(self):
        assert hex_map.biome_label({"map": {"biome": "swamp"}}) == "沼泽"
        assert hex_map.biome_label({}) is None


# ── KP 上下文空间语义注入 ──


def _fixture(with_map: bool):
    scenes = [
        {"id": "a", "title": "镇广场", "kind": "location", "connections": ["b"],
         "keywords": ["广场"]},
        {"id": "b", "title": "老教堂", "kind": "location", "connections": [],
         "keywords": ["教堂"]},
    ]
    if with_map:
        scenes[0]["map"] = {"q": 0, "r": 0, "biome": "urban"}
        scenes[1]["map"] = {"q": 1, "r": -2, "biome": "ruin"}
    module = Module(title="测试镇", rule_system="coc", description="", world_setting={},
                    scenes=scenes, npcs=[], clues=[], triggers=[], handouts=[])
    session = GameSession(module_id="m", status="active", current_scene_id="a",
                          world_state={"visited_scenes": ["a"]})
    pc = Character(name="调查员甲", rule_system="coc", is_player=True,
                   base_attributes={}, skills={}, system_data={})
    return module, session, pc


class TestContextInjection:
    def test_有坐标时连通段带方位与地貌(self):
        module, session, pc = _fixture(with_map=True)
        messages = build_kp_context(session, module, pc, [])
        sys_msg = messages[0]["content"]
        assert "老教堂（北・不远）" in sys_msg
        assert "叙述方向、来路、途经时以此为准" in sys_msg
        assert "【场景地貌】城镇" in sys_msg

    def test_无坐标时保持原有连通段(self):
        module, session, pc = _fixture(with_map=False)
        messages = build_kp_context(session, module, pc, [])
        sys_msg = messages[0]["content"]
        assert "由此可直达：老教堂" in sys_msg
        assert "（北・" not in sys_msg
        assert "【场景地貌】" not in sys_msg


class TestKnownLocationsPayload:
    def test_map字段随已知场景下发(self):
        module, session, pc = _fixture(with_map=True)
        out = session_service.list_known_locations(module, session)
        cur = next(x for x in out if x["id"] == "a")
        assert cur["map"] == {"q": 0, "r": 0, "biome": "urban"}
        assert all("map" in x for x in out)


# ── KP 上帝视角（reveal_all）──


def _three_scene_fixture():
    """a 已访问；b 与 a 相连但未提及（未知）；c 孤立未知。"""
    scenes = [
        {"id": "a", "title": "门厅", "kind": "location", "connections": ["b"],
         "map": {"q": 0, "r": 0, "biome": "interior"}},
        {"id": "b", "title": "地窖", "kind": "location", "connections": [],
         "map": {"q": 1, "r": 0, "biome": "interior"}},
        {"id": "c", "title": "后山", "kind": "location", "connections": [],
         "map": {"q": 4, "r": -2, "biome": "mountain"}},
        {"id": "ch", "title": "尾声", "kind": "chapter"},
    ]
    module = Module(title="M", rule_system="coc", description="", world_setting={},
                    scenes=scenes, npcs=[], clues=[], triggers=[], handouts=[])
    session = GameSession(module_id="m", status="active", current_scene_id="a",
                          world_state={"visited_scenes": ["a"]})
    return module, session


class TestRevealAll:
    def test_玩家侧迷雾不变且known恒真(self):
        module, session = _three_scene_fixture()
        out = session_service.list_known_locations(module, session)
        ids = {x["id"] for x in out}
        assert ids == {"a"}                        # b/c 未知、ch 是章节 → 都不可见
        assert all(x["known"] for x in out)

    def test_KP上帝视角全场景带known标记(self):
        module, session = _three_scene_fixture()
        out = session_service.list_known_locations(module, session, reveal_all=True)
        by_id = {x["id"]: x for x in out}
        assert set(by_id) == {"a", "b", "c"}       # 章节仍不上图
        assert by_id["a"]["known"] is True
        assert by_id["b"]["known"] is False and by_id["c"]["known"] is False
        assert by_id["a"]["connections"] == ["b"]  # KP 侧拓扑完整（不受迷雾过滤）


# ── KP 拖拽落位（set_scene_map）──


class _FakeDb:
    def add(self, obj):
        pass

    def commit(self):
        pass


class TestSetSceneMap:
    def test_移动成功且落新格(self):
        module, _ = _three_scene_fixture()
        new_map = hex_map.set_scene_map(_FakeDb(), module, "b", 2, -1)
        assert new_map == {"q": 2, "r": -1, "biome": "interior"}   # 未给 biome → 保留旧值
        assert next(s for s in module.scenes if s["id"] == "b")["map"]["q"] == 2

    def test_撞格与非法输入拒绝(self):
        import pytest

        module, _ = _three_scene_fixture()
        with pytest.raises(ValueError, match="已被"):
            hex_map.set_scene_map(_FakeDb(), module, "b", 0, 0)     # a 占着 (0,0)
        with pytest.raises(ValueError, match="章节"):
            hex_map.set_scene_map(_FakeDb(), module, "ch", 9, 9)
        with pytest.raises(ValueError, match="不存在"):
            hex_map.set_scene_map(_FakeDb(), module, "nope", 9, 9)
        with pytest.raises(ValueError, match="未知地貌"):
            hex_map.set_scene_map(_FakeDb(), module, "b", 9, 9, biome="太空")

    def test_顺带改地貌(self):
        module, _ = _three_scene_fixture()
        new_map = hex_map.set_scene_map(_FakeDb(), module, "c", 5, -2, biome="ruin")
        assert new_map["biome"] == "ruin"
