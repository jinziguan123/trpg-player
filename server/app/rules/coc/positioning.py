"""战斗方格几何（纯函数、无状态、无 DB）。

把「距离 / 射程 / 近战接触 / 布阵 / 可达格」等空间关系算成给上层消费的原始量或
奖惩骰折算——上层（combat_service）再把奖惩骰喂给既有 resolve_attack/resolve_skill_check，
不新建平行的攻击结算路径。坐标格键统一用字符串 "x,y"（便于进 SQLite JSON 与去重）。

见设计：docs/plans/2026-07-14-战斗方格位置模型-design.md（本文件对应 MVP / P-Grid-1）。
抵近奖励 / 夹击 / 掩体 / 视线属后续阶段（P-Grid-2/3），届时再补。
"""

import math
import re
from collections import deque

from app.rules.coc.combat import resolve_weapon


def _xy(o: dict | None) -> tuple[int, int] | None:
    """从参战方 dict（含 pos）或 {"x","y"} 取整数坐标；无坐标返回 None。"""
    if not o:
        return None
    p = o.get("pos") if "pos" in o else o
    if isinstance(p, dict) and "x" in p and "y" in p:
        return int(p["x"]), int(p["y"])
    return None


def cell_distance(a: dict, b: dict) -> int:
    """两单位（或两坐标）的 Chebyshev 距离（八方向，斜走算 1）。缺坐标 → 大数（视为不可及）。"""
    pa, pb = _xy(a), _xy(b)
    if pa is None or pb is None:
        return 999
    return max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))


def is_adjacent(a: dict, b: dict) -> bool:
    """是否相邻（近战接触距离 = 1，含对角）。"""
    return cell_distance(a, b) <= 1


def range_in_cells(weapon: str | dict, cell_m: float) -> int:
    """武器射程（米）→ 格数。"接触"→1（近战须相邻）；纯数字米按 ceil(米/格边) 折；
    投掷式（"STR/5m" 等无前导数字）MVP 保守回落 5 格。"""
    w = resolve_weapon(weapon) if isinstance(weapon, str) else weapon
    rng = str(w.get("range") or "接触").strip()
    if "接触" in rng:
        return 1
    m = re.match(r"\s*(\d+)", rng)
    if m:
        return max(1, math.ceil(int(m.group(1)) / max(0.1, cell_m)))
    return 5


def range_check(dist: int, range_cells: int, ranged: bool) -> tuple[int, int, bool]:
    """射程判定 →（bonus, penalty, reachable）。
    近战：须相邻（dist≤1）才可达，无奖惩。
    火器：≤射程正常；超基础射程但≤2× → 惩罚骰 1（超程）；>2× → 不可及。"""
    if not ranged:
        return 0, 0, dist <= 1
    if dist <= range_cells:
        return 0, 0, True
    if dist <= 2 * range_cells:
        return 0, 1, True
    return 0, 0, False


def _line_cells(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """两格之间连线经过的格（Bresenham，**不含两端**）——用于视线/掩体遮挡判定。"""
    cells: list[tuple[int, int]] = []
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx - dy
    x, y = x0, y0
    while not (x == x1 and y == y1):
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy
        if (x, y) != (x1, y1):
            cells.append((x, y))
    return cells


def has_line_of_sight(a: dict, b: dict, grid: dict) -> bool:
    """a→b 是否有视线：连线经过 blocked 格或 full 掩体 → 断（射击不可命中）。缺坐标 → 视为有视线。"""
    pa, pb = _xy(a), _xy(b)
    if pa is None or pb is None:
        return True
    blocked = set(grid.get("blocked") or [])
    cover = grid.get("cover") or {}
    for x, y in _line_cells(pa[0], pa[1], pb[0], pb[1]):
        k = f"{x},{y}"
        if k in blocked or cover.get(k) == "full":
            return False
    return True


def cover_penalty(a: dict, b: dict, grid: dict) -> int:
    """a→b 连线经过半掩体（half）→ 命中 -1 惩罚骰（全掩体由 has_line_of_sight 判不可命中）。"""
    pa, pb = _xy(a), _xy(b)
    if pa is None or pb is None:
        return 0
    cover = grid.get("cover") or {}
    for x, y in _line_cells(pa[0], pa[1], pb[0], pb[1]):
        if cover.get(f"{x},{y}") == "half":
            return 1
    return 0


_DOWN = {"dead", "dying", "unconscious", "fled"}


def _can_fight(p: dict) -> bool:
    """仍能参与夹击/被夹击的活跃单位（未死/未濒死/未昏迷/未逃）。"""
    return p.get("hp", 0) > 0 and p.get("status") not in _DOWN


def point_blank_bonus(dist: int, ranged: bool) -> int:
    """抵近射击：火器且距离 ≤2 格 → 命中 +1 奖励骰（近战不吃此项，本就相邻）。"""
    return 1 if (ranged and dist <= 2) else 0


def flank_penalty(defender: dict, participants: list[dict]) -> int:
    """夹击/腹背受敌：与防御者相邻的存活敌方数 adj → 防御检定惩罚骰 max(0, adj-1)，封顶 2。
    首个相邻敌不罚（单挑），第二个起每个 +1。"""
    d_enemy = defender.get("side") == "enemy"
    adj = 0
    for p in participants:
        if p.get("id") == defender.get("id") or not _can_fight(p):
            continue
        if (p.get("side") == "enemy") != d_enemy and is_adjacent(defender, p):
            adj += 1
    return min(2, max(0, adj - 1))


def _place_column(units: list[dict], col: int, rows: int) -> None:
    """把一队单位沿某列 y 轴居中、连续铺开，原地落 pos。n≤rows 不重叠。"""
    n = len(units)
    start = max(0, (rows - n) // 2)
    for i, u in enumerate(units):
        u["pos"] = {"x": col, "y": min(rows - 1, start + i)}


def default_deployment(participants: list[dict], grid: dict) -> None:
    """开战确定性布阵（不掷骰、不读叙事）：我方与敌方各占中央相邻两列（我方 x=cols//2-1、
    敌方 x=cols//2），各自沿 y 居中铺开。原地给每个参战方落 pos。

    MVP 取「相邻紧凑」布阵：双方一上来就在近战接触带，近战可立即开打、NPC 无需走位；
    移动用于走位/拉开/绕后。拉开对峙 + 抵近奖励等留待 P-Grid-2（届时可加开局间隔参数）。"""
    cols, rows = int(grid["cols"]), int(grid["rows"])
    players = [p for p in participants if p.get("side") in ("player", "ally")]
    enemies = [p for p in participants if p.get("side") == "enemy"]
    _place_column(players, cols // 2 - 1, rows)
    _place_column(enemies, cols // 2, rows)


def reachable_cells(start: dict, budget: int, grid: dict, occupied: set[str]) -> set[str]:
    """从 start 的坐标做 BFS，返回步数 ≤ budget 且非 blocked、非 occupied、在盘内的可达格键集合
    （不含起点自身）。八方向移动，每步算 1 格。"""
    xy = _xy(start)
    if xy is None or budget <= 0:
        return set()
    cols, rows = int(grid["cols"]), int(grid["rows"])
    blocked = set(grid.get("blocked") or [])
    seen = {xy: 0}
    q: deque[tuple[int, int]] = deque([xy])
    out: set[str] = set()
    while q:
        x, y = q.popleft()
        d = seen[(x, y)]
        if d >= budget:
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if not (0 <= nx < cols and 0 <= ny < rows):
                    continue
                if (nx, ny) in seen:
                    continue
                key = f"{nx},{ny}"
                if key in blocked or key in occupied:
                    continue
                seen[(nx, ny)] = d + 1
                out.add(key)
                q.append((nx, ny))
    return out
