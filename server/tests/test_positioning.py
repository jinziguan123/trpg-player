"""战斗方格几何纯函数单测（钉死坐标、不掷骰）。对应 MVP / P-Grid-1。"""

from app.rules.coc import positioning as pos


def _u(x, y, side="player"):
    return {"pos": {"x": x, "y": y}, "side": side}


def test_cell_distance_chebyshev():
    assert pos.cell_distance(_u(0, 0), _u(3, 1)) == 3      # 八方向取最大轴距
    assert pos.cell_distance(_u(2, 2), _u(4, 5)) == 3
    assert pos.cell_distance(_u(1, 1), _u(2, 2)) == 1      # 对角相邻
    assert pos.cell_distance({"x": 0, "y": 0}, {"x": 0, "y": 0}) == 0


def test_cell_distance_missing_pos_is_far():
    assert pos.cell_distance({}, _u(1, 1)) == 999


def test_is_adjacent():
    assert pos.is_adjacent(_u(1, 1), _u(2, 2)) is True     # 对角算相邻
    assert pos.is_adjacent(_u(1, 1), _u(1, 2)) is True
    assert pos.is_adjacent(_u(1, 1), _u(3, 1)) is False


def test_range_in_cells():
    assert pos.range_in_cells({"range": "接触"}, 1.5) == 1
    assert pos.range_in_cells({"range": "15"}, 1.5) == 10   # ceil(15/1.5)
    assert pos.range_in_cells({"range": "3"}, 1.5) == 2     # ceil(3/1.5)
    assert pos.range_in_cells({"range": "STR/5m"}, 1.5) == 5  # 投掷式保守回落


def test_range_check_melee_requires_adjacent():
    assert pos.range_check(1, 1, ranged=False) == (0, 0, True)
    assert pos.range_check(2, 1, ranged=False) == (0, 0, False)   # 近战不相邻→够不着


def test_range_check_firearm_over_range_penalty_and_unreachable():
    assert pos.range_check(5, 10, ranged=True) == (0, 0, True)    # 射程内
    assert pos.range_check(15, 10, ranged=True) == (0, 1, True)   # 超基础射程→-1惩罚骰
    assert pos.range_check(25, 10, ranged=True) == (0, 0, False)  # 超2×→不可及


def test_default_deployment_sides_and_no_overlap():
    grid = {"cols": 12, "rows": 8}
    parts = [_u(0, 0, "player"), _u(0, 0, "player"),
             _u(0, 0, "enemy"), _u(0, 0, "enemy"), _u(0, 0, "enemy")]
    pos.default_deployment(parts, grid)
    players = parts[:2]
    enemies = parts[2:]
    assert all(p["pos"]["x"] == 5 for p in players)        # 我方中央左列(cols//2-1)
    assert all(e["pos"]["x"] == 6 for e in enemies)        # 敌方中央右列(cols//2)，与我方相邻
    coords = [(p["pos"]["x"], p["pos"]["y"]) for p in parts]
    assert len(set(coords)) == len(coords)                 # 无重叠
    # 紧凑布阵：我方单位与最近敌方相邻（近战开局可打）
    assert pos.is_adjacent(players[0], enemies[0])


def test_reachable_cells_budget_and_obstacles():
    grid = {"cols": 6, "rows": 6, "blocked": ["2,2"]}
    reach = pos.reachable_cells(_u(0, 0), budget=1, grid=grid, occupied=set())
    assert "1,1" in reach and "1,0" in reach and "0,1" in reach
    assert "3,3" not in reach                              # 超预算
    assert "0,0" not in reach                              # 不含起点
    # 障碍/占用格不可达
    reach2 = pos.reachable_cells(_u(1, 1), budget=1, grid=grid, occupied={"1,2"})
    assert "2,2" not in reach2                             # blocked
    assert "1,2" not in reach2                             # occupied


def test_point_blank_bonus():
    assert pos.point_blank_bonus(1, ranged=True) == 1     # 火器抵近
    assert pos.point_blank_bonus(2, ranged=True) == 1
    assert pos.point_blank_bonus(3, ranged=True) == 0     # 3 格外无奖励
    assert pos.point_blank_bonus(1, ranged=False) == 0    # 近战不吃


def test_flank_penalty():
    hero = {"id": "h", "side": "player", "pos": {"x": 5, "y": 5}, "hp": 10, "status": "ok"}
    e1 = {"id": "e1", "side": "enemy", "pos": {"x": 6, "y": 5}, "hp": 10, "status": "ok"}
    e2 = {"id": "e2", "side": "enemy", "pos": {"x": 4, "y": 5}, "hp": 10, "status": "ok"}
    e3 = {"id": "e3", "side": "enemy", "pos": {"x": 5, "y": 6}, "hp": 10, "status": "ok"}
    assert pos.flank_penalty(hero, [hero, e1]) == 0                  # 单挑不罚
    assert pos.flank_penalty(hero, [hero, e1, e2]) == 1             # 两面夹击 -1
    assert pos.flank_penalty(hero, [hero, e1, e2, e3]) == 2         # 三面 -2（封顶）
    dead = {"id": "e2", "side": "enemy", "pos": {"x": 4, "y": 5}, "hp": 0, "status": "dead"}
    assert pos.flank_penalty(hero, [hero, e1, dead]) == 0           # 死者不计入夹击
    far = {"id": "e2", "side": "enemy", "pos": {"x": 9, "y": 9}, "hp": 10, "status": "ok"}
    assert pos.flank_penalty(hero, [hero, e1, far]) == 0            # 不相邻不计入


def test_reachable_cells_zero_budget_empty():
    assert pos.reachable_cells(_u(0, 0), budget=0, grid={"cols": 4, "rows": 4}, occupied=set()) == set()
