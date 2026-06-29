"""地图生成 spike：验证「AI 把场景描述 → 结构化瓦片网格」是否可行、布局是否像样。

纯数据验证：不渲染、不接前端、不落库。运行：
    cd server && .venv/bin/python scripts/map_spike.py [module_title_substr] [scene_id]
默认跑「陵墓 / scene_2（前室）」。
"""

import asyncio
import json
import logging
import sys

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from app.ai.llm_factory import get_llm  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.module import Module  # noqa: E402

# ── 地图数据模型（v1，叙事级）──────────────────────────────────────────────
# 地形层只用极小的字符集，方便 LLM 产出与人工肉眼检查；物体/出口/NPC 用坐标列表分层，
# 这样 token 可精确放置、可随剧情增量改（复用 flags/状态机）。
LEGEND = {
    "#": "墙",
    ".": "地板",
    "+": "门/出口（落在墙上，对应场景连接）",
    "~": "水",
    ":": "碎石/瓦砾",
    " ": "外部/虚空（房间轮廓之外）",
}

MAP_GEN_PROMPT = """你是 TRPG 像素地图设计师。请把下面这个【场景】转成一张**俯视瓦片网格**（之后会用像素瓦片集做 2.5D 渲染）。
只输出 JSON，结构如下：

{{
  "w": 网格宽(列数, 建议 12-18 的整数),
  "h": 网格高(行数, 建议 9-14 的整数),
  "tiles": ["每行一个长度恰为 w 的字符串", "...共 h 行..."],
  "objects": [{{"name": "家具/道具的中性名,如 石棺/书桌/火盆", "x": 列, "y": 行, "kind": "furniture|item|feature"}}],
  "entrances": [{{"name": "出口名,如 通往甬道", "x": 列, "y": 行, "to": "目标场景id或空"}}],
  "npc_pos": [{{"name": "NPC名", "x": 列, "y": 行}}],
  "notes": "一句话说明你的布局思路（便于人工核验）"
}}

地形字符表（tiles 只能用这些字符）：
{legend}

硬性规则：
1. 房间用墙 `#` 围出轮廓，内部是地板 `.`；轮廓之外用空格 ` `。门/出口用 `+`，落在墙上、数量与「场景连接」对应。
2. 坐标系：x 是列(0..w-1，从左到右)，y 是行(0..h-1，从上到下)。每行字符串长度必须**恰好等于 w**，共 **h** 行。
3. objects / npc_pos 的坐标必须落在**地板 `.`** 上（不能在墙里或虚空里）；entrances 落在门 `+` 上。
4. 依据场景描述合理布局（描述里提到的家具、结构尽量体现）；信息不足就给一个合理的封闭房间。
5. **不要剧透**：objects 用中性名称（"石棺"而非"藏着尸体的石棺"），不写线索真相。
6. 把给定的 NPC / 待放置物体都放进去；没有就给空数组。

【场景】
名称：{name}
描述：{description}
场景连接（这些方向应各有一个门 `+`）：{connections}
在此场景的 NPC（放进 npc_pos）：{npcs}
在此场景的可见物体/道具（放进 objects，用中性名）：{objects}
"""


def pick_scene(title_sub: str, scene_id: str | None):
    db = SessionLocal()
    mod = next(
        (m for m in db.query(Module).all() if title_sub in m.title), None
    )
    if not mod:
        raise SystemExit(f"找不到标题含「{title_sub}」的模组")
    scenes = mod.scenes or []
    scene = (
        next((s for s in scenes if s.get("id") == scene_id), None)
        if scene_id else scenes[0]
    )
    if not scene:
        raise SystemExit(f"模组「{mod.title}」里找不到场景 {scene_id}")
    npcs = [n.get("name", "?") for n in (mod.npcs or []) if n.get("initial_location") == scene.get("id")]
    clues = [c.get("name", "?") for c in (mod.clues or []) if c.get("location") == scene.get("id")]
    return mod, scene, npcs, clues


def _extract_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    return json.loads(s[a:b + 1])


def validate(m: dict) -> list[str]:
    """肉眼之外的机器校验：尺寸一致、坐标在界内且落在地板上。"""
    issues = []
    w, h, tiles = m.get("w"), m.get("h"), m.get("tiles") or []
    if len(tiles) != h:
        issues.append(f"行数 {len(tiles)} ≠ h={h}")
    for i, row in enumerate(tiles):
        if len(row) != w:
            issues.append(f"第 {i} 行长度 {len(row)} ≠ w={w}：{row!r}")

    def cell(x, y):
        if 0 <= y < len(tiles) and 0 <= x < len(tiles[y]):
            return tiles[y][x]
        return None

    for grp, floor_ok in (("objects", "."), ("npc_pos", "."), ("entrances", "+")):
        for it in m.get(grp, []):
            x, y = it.get("x"), it.get("y")
            c = cell(x, y)
            if c is None:
                issues.append(f"{grp} {it.get('name')} 坐标越界 ({x},{y})")
            elif grp == "entrances" and c != "+":
                issues.append(f"出口 {it.get('name')} 不在门 + 上，而在 {c!r} ({x},{y})")
            elif grp != "entrances" and c == "#":
                issues.append(f"{grp} {it.get('name')} 落在墙里 ({x},{y})")
    return issues


def render(m: dict) -> str:
    """把网格 + 物体/出口/NPC 叠加成 ASCII 预览（物体用 ❶❷… 角标，下方列图例）。"""
    tiles = [list(r) for r in (m.get("tiles") or [])]
    marks: list[str] = []
    overlay = {"objects": "①②③④⑤⑥⑦⑧⑨⑩", "npc_pos": "ＡＢＣＤＥＦ", "entrances": "＞"}
    for grp in ("objects", "npc_pos", "entrances"):
        for i, it in enumerate(m.get(grp, [])):
            x, y = it.get("x"), it.get("y")
            sym = overlay[grp][i] if i < len(overlay[grp]) else "?"
            if 0 <= y < len(tiles) and 0 <= x < len(tiles[y]):
                tiles[y][x] = sym
            marks.append(f"  {sym} {grp[:3]}: {it.get('name')} ({x},{y})")
    grid = "\n".join("".join(r) for r in tiles)
    return grid + "\n" + "\n".join(marks)


async def main():
    title_sub = sys.argv[1] if len(sys.argv) > 1 else "陵墓"
    scene_id = sys.argv[2] if len(sys.argv) > 2 else "scene_2"
    mod, scene, npcs, clues = pick_scene(title_sub, scene_id)

    prompt = MAP_GEN_PROMPT.format(
        legend="\n".join(f"  {k!r} = {v}" for k, v in LEGEND.items()),
        name=scene.get("name") or scene.get("title") or scene.get("id"),
        description=scene.get("description") or "(无描述)",
        connections="、".join(scene.get("connections") or []) or "无",
        npcs="、".join(npcs) or "无",
        objects="、".join(clues) or "无（自行据描述布置家具）",
    )

    print(f"=== 场景：{mod.title} / {scene.get('id')} {scene.get('name','')} ===")
    print(f"描述：{scene.get('description')}")
    print(f"连接：{scene.get('connections')}  NPC：{npcs}  物体：{clues}\n")

    llm = get_llm()
    raw = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.4,
        max_tokens=4096,
    )
    try:
        m = _extract_json(raw)
    except Exception as e:
        print("!! JSON 解析失败：", e)
        print(raw[:1500])
        return

    print(render(m))
    print(f"\nnotes: {m.get('notes','')}")
    issues = validate(m)
    print("\n=== 机器校验 ===")
    print("✔ 无结构问题" if not issues else "\n".join("�’ " + s for s in issues))


if __name__ == "__main__":
    asyncio.run(main())
