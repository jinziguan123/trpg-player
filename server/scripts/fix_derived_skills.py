"""一次性数据修正：回填错误的「属性派生技能」（母语、闪避）。

背景
----
母语(=EDU)、闪避(=DEX//2) 依赖角色的具体属性，无法预置在静态默认技能表里。
历史上存在两类错误数据：

* 旧版 ``build_default_skills`` 中 ``skills.get("DEX", 50) // 2`` 永远取默认
  值，导致「不传 skills」路径建出的角色闪避恒为 25；
* 前端手动建卡传入自带 skills、绕过 ``build_default_skills``，使闪避、母语
  停留在静态默认表的 0/缺失。

修正规则（保守且幂等）
----------------------
仅当存储值**小于**应有的派生基础值时才回填到该基础值。绝不下调任何已达到或
超过基础值的技能——那可能是玩家合法投入的技能点。因此重复运行不会再产生改动。

用法
----
    python scripts/fix_derived_skills.py            # 预演（dry-run），只打印不写入
    python scripts/fix_derived_skills.py --apply    # 实际写入
    python scripts/fix_derived_skills.py --apply --db /path/to/trpg.db
"""

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "trpg.db"

# 派生技能规则：技能名 -> (依赖的基础属性, 除数)，基础值 = attrs[attr] // divisor
DERIVED_RULES: dict[str, tuple[str, int]] = {
    "母语": ("EDU", 1),
    "闪避": ("DEX", 2),
}


def plan_fixes(rows: list[sqlite3.Row]) -> list[dict]:
    """计算需要修正的行，返回变更计划。"""
    fixes = []
    for r in rows:
        attrs = json.loads(r["base_attributes"]) if r["base_attributes"] else {}
        skills = json.loads(r["skills"]) if r["skills"] else {}
        changes = []
        for skill, (attr, divisor) in DERIVED_RULES.items():
            attr_val = attrs.get(attr)
            if not isinstance(attr_val, int):
                continue  # 无法计算，跳过
            expected = attr_val // divisor
            cur = skills.get(skill)
            # 仅回填「过低」或缺失的值，保留玩家合法加点（>= 基础值）
            if cur is None or (isinstance(cur, int) and cur < expected):
                skills[skill] = expected
                changes.append((skill, cur, expected))
        if changes:
            fixes.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "changes": changes,
                    "skills_json": json.dumps(skills, ensure_ascii=False),
                }
            )
    return fixes


def main() -> None:
    parser = argparse.ArgumentParser(description="回填错误的属性派生技能（母语/闪避）")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认仅预演）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="trpg.db 路径")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"数据库不存在: {args.db}")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, name, base_attributes, skills FROM characters"
    ).fetchall()
    fixes = plan_fixes(rows)

    print(f"数据库: {args.db}")
    print(f"角色总数: {len(rows)}，需修正: {len(fixes)}")
    for f in fixes:
        detail = "，".join(f"{s} {old} -> {new}" for s, old, new in f["changes"])
        print(f"  - {f['name']}：{detail}")

    if not fixes:
        print("无需修正。")
        con.close()
        return

    if not args.apply:
        print("\n[预演] 未写入。加 --apply 实际执行。")
        con.close()
        return

    for f in fixes:
        con.execute(
            "UPDATE characters SET skills = ? WHERE id = ?",
            (f["skills_json"], f["id"]),
        )
    con.commit()
    con.close()
    print(f"\n[完成] 已更新 {len(fixes)} 个角色。")


if __name__ == "__main__":
    main()
