"""按角色的活库存（``character.system_data.inventory``）：确定性的获取/使用/丢弃/转让。

库存是**权威状态**——不靠 KP 叙述记忆，杜绝「用了你没有的东西 / 用光的再用一次 / 捡的弄丢」。
条目松散：``{id, name, qty, kind?, note?}``。kind 可选：consumable（用一次 qty-1、耗尽移除）/
gear / key / document / weapon —— 只驱动前端图标与「用了消不消耗」。所有写操作就地改
system_data 并提交（fail 由调用方兜底）。效果本身仍由 KP 叙述——本模块只保证「有/没有/几件」可靠。
"""

from __future__ import annotations

import copy
import uuid

from sqlalchemy.orm import Session

from app.models.character import Character

_CONSUMABLE_KINDS = {"consumable"}


def get_inventory(char: Character) -> list[dict]:
    # 深拷贝：调用方在此列表上原地改动不会触及 ORM 挂着的 JSON 值——否则「改旧值 + 再整体赋值」
    # 会被 SQLAlchemy 判为无变化（新旧相等）而不落库，堆叠/增减全部丢失。
    return copy.deepcopy((char.system_data or {}).get("inventory") or [])


def _save(db: Session, char: Character, items: list[dict]) -> None:
    sd = dict(char.system_data or {})
    sd["inventory"] = items
    char.system_data = sd
    db.add(char)
    db.commit()


def find_item(char: Character, item_id: str) -> dict | None:
    return next((it for it in get_inventory(char) if it.get("id") == item_id), None)


def add_item(
    db: Session, char: Character, name: str,
    qty: int = 1, kind: str | None = None, note: str | None = None,
) -> dict:
    """加物品：同名同类型的条目 qty 累加（堆叠），否则新建。返回受影响条目（空 dict 表示无效入参）。"""
    name = (name or "").strip()
    qty = int(qty or 1)
    if not name or qty <= 0:
        return {}
    items = get_inventory(char)
    for it in items:
        if it.get("name") == name and it.get("kind") == kind:
            it["qty"] = int(it.get("qty") or 1) + qty
            _save(db, char, items)
            return it
    entry: dict = {"id": uuid.uuid4().hex, "name": name, "qty": qty}
    if kind:
        entry["kind"] = kind
    if note:
        entry["note"] = note
    items.append(entry)
    _save(db, char, items)
    return entry


def remove_item(db: Session, char: Character, item_id: str, qty: int | None = None) -> dict | None:
    """按 id 移除/减少：qty=None 或 ≥ 现有 → 整条移除；否则减 qty。返回被移除条目快照（qty=实际移除数）。"""
    items = get_inventory(char)
    it = next((x for x in items if x.get("id") == item_id), None)
    if it is None:
        return None
    cur = int(it.get("qty") or 1)
    if qty is None or qty >= cur:
        _save(db, char, [x for x in items if x.get("id") != item_id])
        return {**it, "qty": cur}
    it["qty"] = cur - qty
    _save(db, char, items)
    return {**it, "qty": qty}


def remove_by_name(db: Session, char: Character, name: str, qty: int = 1) -> dict | None:
    """按名字消耗/移除（供 KP 确定性销毁：火把熄灭、绳子被割断等）。匹配不到返回 None。"""
    name = (name or "").strip()
    it = next(
        (x for x in get_inventory(char) if x.get("name") == name or name in (x.get("name") or "")),
        None,
    )
    return remove_item(db, char, it["id"], qty=qty) if it else None


def use_item(db: Session, char: Character, item_id: str) -> dict | None:
    """使用一件：消耗品 qty-1（耗尽移除），非消耗品不减。返回被使用条目快照（含 name/kind）。"""
    it = find_item(char, item_id)
    if it is None:
        return None
    snapshot = dict(it)
    if it.get("kind") in _CONSUMABLE_KINDS:
        remove_item(db, char, item_id, qty=1)
    return snapshot


def give_item(
    db: Session, from_char: Character, to_char: Character, item_id: str, qty: int = 1,
) -> dict | None:
    """把一件（或 qty 件）从 from 转到 to（多人转让）。匹配不到返回 None。"""
    removed = remove_item(db, from_char, item_id, qty=qty)
    if removed is None:
        return None
    add_item(db, to_char, removed["name"], qty=int(removed.get("qty") or 1),
             kind=removed.get("kind"), note=removed.get("note"))
    return removed


def seed_from_equipment(db: Session, char: Character) -> None:
    """开局把角色卡静态 equipment 播种进活库存（仅当库存为空、避免重复播种）。

    equipment 条目可能是字符串或 {name,...}；weapons 不动（战斗仍从 system_data.weapons 读）。
    """
    sd = char.system_data or {}
    if sd.get("inventory"):
        return
    items: list[dict] = []
    for raw in (sd.get("equipment") or []):
        name = raw if isinstance(raw, str) else str((raw or {}).get("name") or "").strip()
        name = (name or "").strip()
        if not name:
            continue
        existing = next((it for it in items if it["name"] == name), None)
        if existing:
            existing["qty"] += 1
        else:
            items.append({"id": uuid.uuid4().hex, "name": name, "qty": 1, "kind": "gear"})
    if items:
        _save(db, char, items)
