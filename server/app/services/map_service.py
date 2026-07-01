"""场景地图生成：把场景描述 → 结构化瓦片网格（AI 自动生成地图特性）。

地形字符层 + 物体/出口/NPC 坐标分层；俯视像素，前端用 Canvas 以伪 2.5D 渲染。
动态变化（打破墙壁出新房间等）复用场景 states/flags 机制，不在此处理。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.ai.llm_factory import get_llm
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import session_service

logger = logging.getLogger(__name__)

# 地形字符表（tiles 只能用这些字符）——极小字符集，便于 LLM 产出与渲染映射。
LEGEND = {
    "#": "墙",
    ".": "地板",
    "+": "门/出口（落在墙上，对应场景连接）",
    "~": "水",
    ":": "碎石/瓦砾",
    " ": "外部/虚空（房间轮廓之外）",
}

MAP_GEN_PROMPT = """你是 TRPG 像素地图设计师。请把下面这个【场景】转成一张**俯视瓦片网格**（前端会用像素瓦片以伪 2.5D 渲染）。
只输出 JSON，结构如下：

{{
  "w": 网格宽(列数, 12-18 的整数),
  "h": 网格高(行数, 9-14 的整数),
  "tiles": ["每行一个长度恰为 w 的字符串", "...共 h 行..."],
  "objects": [{{"name": "家具/道具的中性名,如 石棺/书桌/火盆", "x": 列, "y": 行, "kind": "furniture|item|feature", "asset_id": "可选，见下方素材库表"}}],
  "entrances": [{{"name": "出口名,如 通往前室", "x": 列, "y": 行, "to": "目标场景id（从下方连接表里选）"}}],
  "npc_pos": [{{"name": "NPC名", "x": 列, "y": 行, "hostile": false, "asset_id": "可选，见下方素材库表"}}],
  "notes": "一句话说明布局思路（便于人工核验）"
}}

地形字符表（tiles 只能用这些字符）：
{legend}

硬性规则：
1. 房间用墙 `#` 围出轮廓，内部地板 `.`，轮廓之外用空格 ` `。门 `+` 落在墙上，**数量与可通行的连接对应**。
2. 坐标系：x 是列(0..w-1，左→右)，y 是行(0..h-1，上→下)。每行字符串长度必须**恰好等于 w**，共 **h** 行。
3. objects/npc_pos 坐标必须落在**地板 `.`** 上（不能在墙或虚空里）；entrances 落在门 `+` 上，并把 `to` 填成对应的目标场景 id。
4. 依据场景描述合理布局（描述里提到的家具、结构尽量体现）；信息不足就给一个合理的封闭房间。
5. **只画"此刻可见、可通行"的内容**：被封死/需要破坏才能通的拱门，画成**墙**而非门（之后由剧情解锁）；
   需要调查才发现的隐藏壁龛/暗格，**不要**画进基础地图。这样可避免剧透、也便于随剧情增量揭开。
6. **不剧透**：objects 用中性名称（"石棺"而非"藏着尸体的石棺"），不写线索真相。
7. 把给定的 NPC 放进 npc_pos；可见的家具/道具放进 objects；没有就给空数组。
8. 敌对生物/怪物（会攻击玩家的）在 npc_pos 里标 "hostile": true，便于地图上用敌人样式区分；普通 NPC 为 false。
9. 素材库：下表是已有的可用素材（格式 [类型] id｜名称）。若某 object/NPC 与表中某素材**语义匹配**
   （按名称/类型，如「石棺」对应素材库里的石棺），就把它的 "asset_id" 填成该素材的 id，让地图用上对应贴图；
   拿不准或无匹配就**不要填 asset_id**，系统会按类型取默认素材。terrain（地板/墙/门）不用填，按类型默认。
10. **多层建筑（重要）**：若该地点含多个楼层/层级（如楼上/楼下/阁楼/地下室），**绝不要**把多层塞进同一张网格；
    改为**每层各出一张地图**，返回：{{"floors": [{{"name": "一楼", "w":.., "h":.., "tiles":[..], "objects":[..], "entrances":[..], "npc_pos":[..], "notes":".."}}, {{"name": "地下室", ...}}]}}。
    每层内部按上面的规则各自成图；楼梯在该层画成一个出口（entrances，name 写「上楼」「下楼」，to 留空）。**单层场景照常返回单张地图**（不要用 floors）。

【场景】
名称：{name}
描述：{description}
可通行连接（每个给一个门 `+`，entrances.to 用括号里的 id）：{connections}
在此场景的 NPC（放进 npc_pos）：{npcs}
在此场景的可见物体/道具（放进 objects，中性名）：{objects}

【素材库可用素材】
{asset_catalog}
"""


VARIANT_MAP_PROMPT = """据下面的【基础地图】生成一张【变体地图】，体现这一剧情变化：{hint}

只输出 JSON，结构与基础地图相同（w/h/tiles/objects/entrances/npc_pos/notes）。要求：
1. w/h 必须与基础地图**完全一致**；在原布局上做**局部**改动，不要重画整张：
   - 打通/打破墙：把对应的 `#` 改成 `+`（门）或 `.`（地板）；
   - 露出新房间：把原本是墙/虚空（# 或空格）的位置补成地板 `.` 并围上墙 `#`；
   - NPC/物体移动或出现/消失：改 npc_pos / objects 的坐标或增删。
2. 坐标规则同基础地图：tiles 每行长度=w、共 h 行；objects/npc_pos 落在地板，entrances 落在门。
3. 不剧透：名称中性。其余未变化的部分尽量与基础地图保持一致。

地形字符表：
{legend}

【基础地图】：
{base_json}
"""


VISION_MAP_PROMPT = """这是一张 TRPG 模组自带的【场景地图图片】。请把它转译成我们的瓦片网格 JSON。
只输出 JSON：{{"w":整数,"h":整数,"tiles":["每行长度=w 的字符串",...共 h 行],
"objects":[{{"name":中性名,"x":列,"y":行,"kind":"furniture|item|feature"}}],
"entrances":[{{"name":出口名,"x":列,"y":行}}],"npc_pos":[{{"name":,"x":,"y":,"hostile":false}}],"notes":"一句话说明"}}

把图中的墙画成 `#`、地面 `.`、门/通道 `+`、水 `~`、碎石瓦砾 `:`、空白区域用空格；
按图中相对位置与比例选合适的 w/h（建议 12-20 × 9-16）。图中标注的家具/出口/起点尽量放进 objects/entrances。
地形字符表：
{legend}

参考场景：{name}（{description}）。不剧透，名称用中性词。"""


async def generate_map_from_image(image_bytes: bytes, mime: str, scene: dict) -> dict:
    """多模态：据模组自带的地图图片生成本项目的瓦片地图（需当前 LLM 支持视觉）。"""
    import base64
    llm = get_llm()
    if not llm.supports_vision():
        raise ValueError("当前模型不支持多模态（无法据图片生成地图）。请在设置里切换到支持视觉的模型（如 GPT-4o / Claude / Gemini / Qwen-VL）。")
    prompt = VISION_MAP_PROMPT.format(
        legend="\n".join(f"  {k!r} = {v}" for k, v in LEGEND.items()),
        name=_scene_label(scene), description=scene.get("description") or "(无描述)",
    )
    raw = await llm.complete_vision(prompt, [(base64.b64encode(image_bytes).decode(), mime)], max_tokens=4096)
    m = _extract_json(raw)
    issues = validate_map(m)
    if issues:
        m["_issues"] = issues
    return m


async def generate_variant_map(base_map: dict, hint: str) -> dict:
    """据基础地图 + 一句变化说明，让 LLM 产出同尺寸的变体地图（用于 flag 触发的地图改变）。"""
    llm = get_llm()
    raw = await llm.complete(
        messages=[{"role": "user", "content": VARIANT_MAP_PROMPT.format(
            hint=hint or "（未指定，按剧情合理推断一处变化）",
            legend="\n".join(f"  {k!r} = {v}" for k, v in LEGEND.items()),
            base_json=json.dumps({k: base_map.get(k) for k in ("w", "h", "tiles", "objects", "entrances", "npc_pos")}, ensure_ascii=False),
        )}],
        response_format={"type": "json_object"},
        temperature=0.4,
        max_tokens=4096,
    )
    m = _extract_json(raw)
    issues = validate_map(m)
    if issues:
        m["_issues"] = issues
    return m


def _scene_label(scene: dict) -> str:
    return scene.get("name") or scene.get("title") or scene.get("id") or "(未命名)"


def _format_asset_catalog(assets: list[dict] | None) -> str:
    """素材库目录给 AI 选用：[类型] id｜名称（只列可放置层，地形按默认不必选）。"""
    rows = [a for a in (assets or []) if a.get("kind") not in (None, "floor", "wall", "door", "water", "rubble")]
    if not rows:
        return "（素材库暂无可选物体/NPC 素材；不要填 asset_id）"
    return "\n".join(f"  [{a.get('kind')}] {a.get('id')}｜{a.get('name')}" for a in rows[:60])


def build_map_prompt(scene: dict, npcs: list[str], clues: list[str], scene_names: dict[str, str], assets: list[dict] | None = None) -> str:
    conns = scene.get("connections") or []
    conn_str = "、".join(f"{scene_names.get(cid, cid)}（{cid}）" for cid in conns) or "无"
    return MAP_GEN_PROMPT.format(
        legend="\n".join(f"  {k!r} = {v}" for k, v in LEGEND.items()),
        name=_scene_label(scene),
        description=scene.get("description") or "(无描述)",
        connections=conn_str,
        npcs="、".join(npcs) or "无",
        objects="、".join(clues) or "无（自行据描述布置家具）",
        asset_catalog=_format_asset_catalog(assets),
    )


def _extract_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    return json.loads(s[a:b + 1])


def validate_map(m: dict) -> list[str]:
    """机器校验：尺寸一致、坐标在界内、物体/NPC 落在地板、出口落在门。返回问题列表（空=通过）。"""
    issues: list[str] = []
    w, h, tiles = m.get("w"), m.get("h"), m.get("tiles") or []
    if not isinstance(w, int) or not isinstance(h, int):
        return ["缺少有效的 w/h"]
    if len(tiles) != h:
        issues.append(f"行数 {len(tiles)} ≠ h={h}")
    for i, row in enumerate(tiles):
        if not isinstance(row, str) or len(row) != w:
            issues.append(f"第 {i} 行长度异常（应为 w={w}）")

    def cell(x, y):
        if isinstance(y, int) and isinstance(x, int) and 0 <= y < len(tiles) and 0 <= x < len(tiles[y]):
            return tiles[y][x]
        return None

    for grp in ("objects", "npc_pos", "entrances"):
        for it in m.get(grp, []) or []:
            c = cell(it.get("x"), it.get("y"))
            if c is None:
                issues.append(f"{grp} {it.get('name')} 坐标越界 ({it.get('x')},{it.get('y')})")
            elif grp == "entrances" and c != "+":
                issues.append(f"出口 {it.get('name')} 未落在门 + 上（在 {c!r}）")
            elif grp != "entrances" and c == "#":
                issues.append(f"{grp} {it.get('name')} 落在墙里")
    return issues


def _clean_asset_ids(m: dict, asset_ids: set[str]) -> None:
    """剔除 AI 编造的、库里不存在的 asset_id（保留有效的让地图用上对应素材）。"""
    for grp in ("objects", "npc_pos"):
        for it in m.get(grp, []) or []:
            aid = it.get("asset_id")
            if aid and aid not in asset_ids:
                it.pop("asset_id", None)


async def generate_scene_map(
    scene: dict, npcs: list[str], clues: list[str], scene_names: dict[str, str],
    assets: list[dict] | None = None,
) -> dict:
    """调用 LLM 为单个场景生成地图（含一次校验，问题随 map._issues 返回，不抛错）。
    assets 给定时把素材库目录喂给 AI，让它为匹配的物体/NPC 填 asset_id（自动用上库内素材）。"""
    llm = get_llm()
    raw = await llm.complete(
        messages=[{"role": "user", "content": build_map_prompt(scene, npcs, clues, scene_names, assets)}],
        response_format={"type": "json_object"},
        temperature=0.4,
        max_tokens=4096,
    )
    data = _extract_json(raw)
    ids = {a.get("id") for a in (assets or [])}

    def _finish(m: dict) -> dict:
        _clean_asset_ids(m, ids)
        issues = validate_map(m)
        if issues:
            m["_issues"] = issues
            logger.warning("场景 %s 地图校验问题：%s", scene.get("id"), issues)
        return m

    floors = data.get("floors")
    if isinstance(floors, list) and floors:
        # 多层建筑：每层各一张图（楼层字段可能内联，也可能嵌在 map 下）
        out = []
        for i, f in enumerate(floors):
            fm = f.get("map") if isinstance(f.get("map"), dict) else {
                k: f[k] for k in ("w", "h", "tiles", "objects", "entrances", "npc_pos", "notes") if k in f
            }
            out.append({"name": f.get("name") or f"第 {i + 1} 层", "map": _finish(fm)})
        return {"floors": out}
    return _finish(data)


def _spawn_pos(m: dict) -> tuple[int, int]:
    """玩家在场景里的落点：优先第一个出口（入口），否则第一块地板，再否则 (0,0)。"""
    ents = m.get("entrances") or []
    if ents and isinstance(ents[0].get("x"), int):
        return ents[0]["x"], ents[0].get("y", 0)
    for y, row in enumerate(m.get("tiles") or []):
        x = row.find(".")
        if x >= 0:
            return x, y
    return 0, 0


def _is_floor(m: dict, x: int, y: int) -> bool:
    tiles = m.get("tiles") or []
    if 0 <= y < len(tiles) and 0 <= x < len(tiles[y]):
        return tiles[y][x] in "."
    return False


def _nearest_floor(m: dict, x: int, y: int, occupied: set[tuple[int, int]]) -> tuple[int, int]:
    """从 (x,y) 起向外找最近的、未被占用的地板格；找不到就退回 (x,y)。"""
    if _is_floor(m, x, y) and (x, y) not in occupied:
        return x, y
    w, h = m.get("w", 0), m.get("h", 0)
    for r in range(1, max(w, h) + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = x + dx, y + dy
                if _is_floor(m, nx, ny) and (nx, ny) not in occupied:
                    return nx, ny
    return x, y


def _resolve_anchor(m: dict, name: str, tracked: dict) -> tuple[int, int] | None:
    """把 [MOVE] 的 to=<锚点> 解析为坐标：支持 已追踪角色名 / 出口 / 物体 / npc_pos 名 / "x,y"。"""
    name = (name or "").strip()
    if not name:
        return None
    # 已追踪的其他角色/NPC
    if name in tracked and isinstance(tracked[name], (list, tuple)) and len(tracked[name]) == 2:
        return int(tracked[name][0]), int(tracked[name][1])
    # 地图上的命名锚点（出口/物体/npc_pos）
    for grp in ("entrances", "objects", "npc_pos"):
        for it in m.get(grp) or []:
            nm = str(it.get("name", ""))
            if nm and (nm == name or name in nm or nm in name) and isinstance(it.get("x"), int):
                return it["x"], it.get("y", 0)
    # "x,y" 字面量
    if "," in name:
        parts = name.replace("，", ",").split(",")
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None
    return None


def scene_floor_maps(scene: dict) -> list[dict]:
    """规整场景的楼层地图列表 → [{"name", "map"}]。

    多层建筑用 scene['floors']（如科比特的房子＝二楼/一楼/地下室各一张图）；
    单层场景回落到 scene['map']（名称留空）。无地图返回 []。
    """
    floors = scene.get("floors")
    if isinstance(floors, list) and floors:
        out = []
        for i, f in enumerate(floors):
            if isinstance(f, dict) and f.get("map"):
                out.append({"name": f.get("name") or f"第 {i + 1} 层", "map": f["map"]})
        if out:
            return out
    if scene.get("map"):
        return [{"name": "", "map": scene["map"]}]
    return []


def _resolved_scene_floors(db: Session, session: GameSession, scene_id: str | None = None):
    """(scene, [{"name","map"}]) —— 按当前 flags 解析指定场景（默认会话当前场景）及其楼层地图。"""
    from app.ai.context import _active_flags, _resolve_state

    module = db.get(Module, session.module_id)
    scenes = (module.scenes if module else []) or []
    target_id = scene_id or session.current_scene_id
    scene = next(
        (s for s in scenes if s.get("id") == target_id),
        scenes[0] if scenes else None,
    )
    if not scene:
        return None, []
    resolved = _resolve_state(scene, _active_flags(session))
    return scene, scene_floor_maps(resolved)


def _resolved_scene_map(db: Session, session: GameSession, scene_id: str | None = None):
    """(scene, resolved_map) —— 取解析后的首层地图（供 [MOVE] 走位等按单图处理）。"""
    scene, floors = _resolved_scene_floors(db, session, scene_id)
    return scene, (floors[0]["map"] if floors else None)


def apply_move(db: Session, session: GameSession, actor: str, target: str) -> None:
    """处理 KP 的 [MOVE: actor, to]：把锚点解析成坐标、贴最近空地板，落库到 world_state.positions。"""
    actor = (actor or "").strip()
    scene, m = _resolved_scene_map(db, session)
    if not (actor and scene and m):
        return
    scene_id = scene.get("id")
    ws = session.world_state or {}
    tracked = dict((ws.get("positions") or {}).get(scene_id) or {})
    anchor = _resolve_anchor(m, target, tracked)
    if anchor is None:
        return
    occupied = {(int(p[0]), int(p[1])) for p in tracked.values()
                if isinstance(p, (list, tuple)) and len(p) == 2 and p != tracked.get(actor)}
    x, y = _nearest_floor(m, anchor[0], anchor[1], occupied)
    session_service.set_position(db, session.id, scene_id, actor, x, y)


def _floor_entities(
    db: Session, session: GameSession, m: dict, scene_id: str, include_party: bool,
) -> list[dict]:
    """算一张（楼层）地图上的实体位置：NPC 按该层 npc_pos，队伍成员仅在入口层摆放。"""
    entities: list[dict] = []
    if not m:
        return entities
    tracked = dict((session.world_state or {}).get("positions", {}).get(scene_id) or {})
    occupied: set[tuple[int, int]] = set()
    locations = session_service.get_party_locations(session)

    def here(cid: str) -> bool:
        # 无位置记录的成员，回落视作在会话当前场景（向后兼容旧存档）
        return locations.get(cid, session.current_scene_id) == scene_id

    def place(name: str, kind: str, default_xy: tuple[int, int], asset_id=None):
        if name in tracked and len(tracked[name]) == 2:
            x, y = int(tracked[name][0]), int(tracked[name][1])
        elif tuple(default_xy) not in occupied:
            x, y = int(default_xy[0]), int(default_xy[1])
        else:
            x, y = _nearest_floor(m, default_xy[0], default_xy[1], occupied)
        occupied.add((x, y))
        ent = {"name": name, "x": x, "y": y, "kind": kind}
        if asset_id:
            ent["asset_id"] = asset_id
        entities.append(ent)

    sx, sy = _spawn_pos(m)
    if include_party:
        pc = db.get(Character, session.player_character_id)
        if pc and here(pc.id):
            place(pc.name, "player", (sx, sy))
        for t in session_service.get_party_members(db, session.id, exclude_id=session.player_character_id):
            if here(t.id):
                place(t.name, "ally", (sx, sy))
    for n in m.get("npc_pos") or []:
        nm = str(n.get("name", "")).strip()
        if not nm:
            continue
        kind = "enemy" if n.get("hostile") else "npc"
        place(nm, kind, (int(n.get("x", sx)), int(n.get("y", sy))), n.get("asset_id"))
    return entities


def current_scene_map(db: Session, session: GameSession, char_id: str | None = None) -> dict:
    """运行时：返回某角色所在场景的**分层**地图 + 各层实体位置。

    ``char_id`` 给定时（前端按当前用户拉取）取该角色所在场景——分头行动时各看各的。
    多层建筑（floors）返回多张楼层图，前端加楼层切换；单层返回单元素列表。队伍成员统一
    摆在「入口层」（有出口的那层，否则第 0 层），各层 NPC 按其 npc_pos。
    """
    view_scene_id = session_service.get_char_location(session, char_id)
    scene, floors = _resolved_scene_floors(db, session, scene_id=view_scene_id)
    if not scene:
        return {"scene_id": None, "scene_name": None, "floors": []}
    scene_id = scene.get("id")
    entry_idx = next((i for i, f in enumerate(floors) if (f["map"].get("entrances"))), 0)
    out_floors = []
    for i, f in enumerate(floors):
        ents = _floor_entities(db, session, f["map"], scene_id, include_party=(i == entry_idx))
        out_floors.append({
            "name": f["name"],
            "map": {**f["map"], "npc_pos": []},  # NPC 已并入 entities，避免重复画
            "entities": ents,
        })
    return {
        "scene_id": scene_id,
        "scene_name": scene.get("name") or scene.get("title") or scene.get("id"),
        "floors": out_floors,
    }


async def generate_maps_for_module(db: Session, module_id: str, force: bool = False) -> Module | None:
    """为模组所有场景生成并落库地图（已有 map 的场景默认跳过，force=True 全部重生成）。"""
    module = db.get(Module, module_id)
    if not module:
        return None
    from app.models.asset import Asset
    assets = [{"id": a.id, "name": a.name, "kind": a.kind} for a in db.query(Asset).all()]
    scene_names = {s.get("id"): _scene_label(s) for s in (module.scenes or [])}
    new_scenes = []
    for s in (module.scenes or []):
        s = dict(s)
        if force or not (s.get("map") or s.get("floors")):
            npcs = [n.get("name", "?") for n in (module.npcs or []) if n.get("initial_location") == s.get("id")]
            clues = [c.get("name", "?") for c in (module.clues or []) if c.get("location") == s.get("id")]
            try:
                result = await generate_scene_map(s, npcs, clues, scene_names, assets)
                if isinstance(result, dict) and result.get("floors"):
                    s["floors"] = result["floors"]  # 多层建筑：各层一张图
                    s.pop("map", None)
                else:
                    s["map"] = result
                    s.pop("floors", None)
            except Exception:
                logger.exception("生成场景地图失败：scene=%s", s.get("id"))
        # 为「结构性」状态自动生成变体地图（多层场景暂不做变体，规避复杂度）
        if s.get("map") and not s.get("floors"):
            states = []
            for st in (s.get("states") or []):
                st = dict(st)
                if st.get("structural") and (force or not st.get("map")):
                    hint = f"{'/'.join(st.get('when') or [])}：{st.get('description') or st.get('atmosphere') or '场景结构发生改变'}"
                    try:
                        st["map"] = await generate_variant_map(s["map"], hint)
                    except Exception:
                        logger.exception("生成变体地图失败：scene=%s when=%s", s.get("id"), st.get("when"))
                states.append(st)
            if states:
                s["states"] = states
        new_scenes.append(s)
    module.scenes = new_scenes
    db.commit()
    db.refresh(module)
    return module
