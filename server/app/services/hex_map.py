"""六边形沙盘：axial 坐标数学 + 场景落位管线（AI 提议 → 确定性修复 → KP 修正）。

场景间地理位置存在 scene["map"] = {"q", "r", "biome"}（pointy-top axial：东为 +q，
南为 +r 的屏幕方向；正北落在 (q, r) 的 (+1, -2) 一线）。坐标是**象征性相对位置**：
只承诺方位与相对远近，不承诺比例尺；travel 仍走 connections 图校验，本模块不改变
移动规则。场景内部几何（房间/墙）是既定红线，勿在此扩展。

修复原则「只补洞、不推翻」：LLM 解析或 KP 拖拽给出的合法坐标一律保留；缺失/冲突的
场景按「已定位邻居重心就近 + 螺旋找空位」确定性落位（同输入同输出）。存量模组无
map 字段 → 同一修复器懒回填（ensure_module_map，幂等）。
"""

from __future__ import annotations

import math

BIOMES = (
    "plain", "forest", "water", "coast", "desert",
    "mountain", "swamp", "urban", "ruin", "interior",
)
BIOME_LABELS = {
    "plain": "原野", "forest": "密林", "water": "水域", "coast": "海岸",
    "desert": "荒漠", "mountain": "山地", "swamp": "沼泽", "urban": "城镇",
    "ruin": "废墟", "interior": "室内",
}

# pointy-top axial 的六个邻接方向（环绕一圈，顺序只需确定性）
_DIRS = ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))

_DIR_WORDS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


def axial_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def _to_pixel(q: float, r: float) -> tuple[float, float]:
    """axial → 屏幕像素方向（x 向东，y 向南），只用于算方位角。"""
    return q + r / 2, r * math.sqrt(3) / 2


def direction_word(frm: tuple[int, int], to: tuple[int, int]) -> str:
    """八方位词：以正北为 0° 顺时针分扇区。同格返回空串。"""
    x0, y0 = _to_pixel(*frm)
    x1, y1 = _to_pixel(*to)
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return ""
    bearing = math.degrees(math.atan2(dx, -dy)) % 360
    return _DIR_WORDS[round(bearing / 45) % 8]


def distance_word(d: int) -> str:
    if d <= 0:
        return "同处"
    if d == 1:
        return "紧邻"
    if d <= 3:
        return "不远"
    if d <= 6:
        return "有些路程"
    return "相当远"


def scene_coord(scene: dict | None) -> tuple[int, int] | None:
    """场景的合法坐标；缺失/非整数返回 None（fail-open）。"""
    m = (scene or {}).get("map")
    if not isinstance(m, dict):
        return None
    try:
        return int(m["q"]), int(m["r"])
    except (KeyError, TypeError, ValueError):
        return None


def _axial_round(qf: float, rf: float) -> tuple[int, int]:
    """cube 取整（q+r+s=0）：把重心浮点坐标收敛到最近的合法 hex。"""
    sf = -qf - rf
    q, r, s = round(qf), round(rf), round(sf)
    dq, dr, ds = abs(q - qf), abs(r - rf), abs(s - sf)
    if dq > dr and dq > ds:
        q = -r - s
    elif dr > ds:
        r = -q - s
    return int(q), int(r)


def _spiral(center: tuple[int, int], max_radius: int = 128):
    """从 center 向外按环遍历（确定性顺序）。max_radius=128 覆盖 4 万+ 格，实际用不满。"""
    yield center
    cq, cr = center
    for k in range(1, max_radius + 1):
        q, r = cq + _DIRS[4][0] * k, cr + _DIRS[4][1] * k
        for d in range(6):
            for _ in range(k):
                yield (q, r)
                q += _DIRS[d][0]
                r += _DIRS[d][1]


def ensure_scene_maps(scenes: list) -> bool:
    """校验并修复全部 location 场景的 map（原地改 scene dict）。返回是否有改动。

    - 合法提议保留：整数 q/r 且未与先前场景撞格（列表序先到先得）；
    - 缺失/冲突者重新落位：已定位邻居最多的先放，落在邻居重心最近的空格；
      无已定位邻居则落全图重心附近；全图空白则第一个放原点；
    - biome 归一到枚举（小写），缺失/非法默认 plain；
    - chapter 场景不上图，误给的 map 清掉。
    """
    locs = [s for s in scenes or [] if isinstance(s, dict) and s.get("kind") != "chapter"]
    changed = False
    for s in scenes or []:
        if isinstance(s, dict) and s.get("kind") == "chapter" and "map" in s:
            s.pop("map", None)
            changed = True
    if not locs:
        return changed

    coord_of: dict[int, tuple[int, int]] = {}   # locs 下标 → 坐标
    used: set[tuple[int, int]] = set()
    todo: list[int] = []
    for i, s in enumerate(locs):
        c = scene_coord(s)
        if c is not None and c not in used:
            coord_of[i] = c
            used.add(c)
        else:
            todo.append(i)

    idx_by_id = {str(s.get("id")): i for i, s in enumerate(locs) if s.get("id")}
    adj: dict[int, set[int]] = {i: set() for i in range(len(locs))}
    for i, s in enumerate(locs):
        for c in s.get("connections") or []:
            j = idx_by_id.get(str(c or "").strip())
            if j is not None and j != i:
                adj[i].add(j)
                adj[j].add(i)

    while todo:
        # 已定位邻居最多者优先（并列取列表序）——让链式结构沿着已放好的一端生长
        todo.sort(key=lambda i: (-sum(1 for j in adj[i] if j in coord_of), i))
        i = todo.pop(0)
        placed_nb = [coord_of[j] for j in adj[i] if j in coord_of]
        anchor = placed_nb or list(coord_of.values())
        if anchor:
            target = _axial_round(
                sum(c[0] for c in anchor) / len(anchor),
                sum(c[1] for c in anchor) / len(anchor),
            )
        else:
            target = (0, 0)
        spot = next(c for c in _spiral(target) if c not in used)
        coord_of[i] = spot
        used.add(spot)
        changed = True

    for i, s in enumerate(locs):
        q, r = coord_of[i]
        m = s.get("map") if isinstance(s.get("map"), dict) else {}
        biome = str(m.get("biome") or "").strip().lower()
        if biome not in BIOMES:
            biome = "plain"
        new_map = {"q": q, "r": r, "biome": biome}
        if s.get("map") != new_map:
            s["map"] = new_map
            changed = True
    return changed


def ensure_module_map(db, module) -> bool:
    """存量模组懒回填：scenes 过修复器，有改动才落库（幂等；JSON 列须整体重赋值）。"""
    scenes = [dict(s) if isinstance(s, dict) else s for s in (module.scenes or [])]
    if not ensure_scene_maps(scenes):
        return False
    module.scenes = scenes
    db.add(module)
    db.commit()
    return True


def set_scene_map(db, module, scene_id: str, q: int, r: int, biome: str | None = None) -> dict:
    """KP 拖拽落位：把指定场景移到 (q, r)，可顺带改地貌。

    校验后整体重赋值 scenes（JSON 列）并落库。非法情形抛 ValueError（调用方转 400）：
    场景不存在 / chapter 不上沙盘 / 目标格已被占 / 显式给了未知地貌。
    """
    scenes = [dict(s) if isinstance(s, dict) else s for s in (module.scenes or [])]
    target = next(
        (s for s in scenes if isinstance(s, dict) and s.get("id") == scene_id), None,
    )
    if target is None:
        raise ValueError("场景不存在")
    if target.get("kind") == "chapter":
        raise ValueError("章节场景不上沙盘")
    q, r = int(q), int(r)
    for s in scenes:
        if isinstance(s, dict) and s.get("id") != scene_id and scene_coord(s) == (q, r):
            raise ValueError(f"该格已被「{s.get('title') or s.get('id')}」占用")
    old = target.get("map") if isinstance(target.get("map"), dict) else {}
    if biome is not None:
        b = str(biome).strip().lower()
        if b not in BIOMES:
            raise ValueError(f"未知地貌：{biome}")
    else:
        b = str(old.get("biome") or "").strip().lower()
        if b not in BIOMES:
            b = "plain"
    target["map"] = {"q": q, "r": r, "biome": b}
    module.scenes = scenes
    db.add(module)
    db.commit()
    return target["map"]


def neighbor_label(cur_scene: dict | None, nb_scene: dict | None) -> str | None:
    """「北・紧邻」式方位标签；任一侧无坐标返回 None（旧模组 fail-open，不阻塞）。"""
    a, b = scene_coord(cur_scene), scene_coord(nb_scene)
    if a is None or b is None or a == b:
        return None
    return f"{direction_word(a, b)}・{distance_word(axial_distance(a, b))}"


def biome_label(scene: dict | None) -> str | None:
    m = (scene or {}).get("map")
    if not isinstance(m, dict):
        return None
    return BIOME_LABELS.get(str(m.get("biome") or "").strip().lower())
