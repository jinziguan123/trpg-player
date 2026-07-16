"""world_state 读写适配器 + schema 版本边界。

`GameSession.world_state` 是一块散落被各处直接 `dict(ws)` 浅拷贝 + 整段 JSON 重赋值的 JSON——
两类坑反复出现：
  ① 读出引用后**原地改嵌套值**再重赋同一对象 → SQLAlchemy 判「无变化」不落库（战斗态曾中招）；
  ② 浅拷贝顶层、嵌套仍是共享引用 → 改动会漏到 ORM 挂着的旧值。

本模块给出**唯一正确的读写口径**（新代码一律走这里；旧调用点可增量迁移，不必一次拆完）：
- `read`   —— 深拷贝快照，调用方随便改不碰 ORM。
- `set_key`—— 写一个顶层键（None=删除），浅拷贝顶层 + 整体重赋值 + 盖版本号 + commit。
- `mutate` —— 读-改-写：对**深拷贝**调 fn(ws) 就地改，再整体重赋值 —— 嵌套改动一定被 ORM 视为变化。
- `get`    —— 便捷只读。

`SCHEMA_VERSION` + `migrate`：写入时盖当前版本号，为将来的字段迁移留统一入口（当前 v1 无迁移）。
"""

import copy

from sqlalchemy.orm import Session

from app.models.session import GameSession

SCHEMA_VERSION = 1


def migrate(ws: dict) -> dict:
    """把旧版 world_state 原地升到 SCHEMA_VERSION（当前 v1 仅盖版本号；将来在此按 from-version 迁移）。"""
    # v = ws.get("schema_version", 0)
    # if v < 2: ...  # 将来的迁移写在这里
    ws["schema_version"] = SCHEMA_VERSION
    return ws


def read(session: GameSession) -> dict:
    """world_state 的深拷贝快照。调用方原地改动不会误触 ORM 挂着的 JSON（读写解耦）。"""
    return copy.deepcopy(dict(session.world_state or {}))


def get(session: GameSession, key: str, default=None):
    """便捷只读某顶层键（不拷贝、不落库）。需要改动请用 set_key/mutate。"""
    return (session.world_state or {}).get(key, default)


def set_key(db: Session, session: GameSession, key: str, value) -> None:
    """写一个顶层键（value=None → 删除该键）：浅拷贝顶层 + 整体重赋值 + 盖版本号 + commit。

    顶层键的整体替换用浅拷贝即可安全落库（新对象引用触发 ORM 变更检测）；若要**原地改嵌套值**
    请改用 mutate（深拷贝），否则可能不落库。"""
    ws = dict(session.world_state or {})
    if value is None:
        ws.pop(key, None)
    else:
        ws[key] = value
    ws["schema_version"] = SCHEMA_VERSION
    session.world_state = ws
    db.commit()


def mutate(db: Session, session: GameSession, fn) -> None:
    """读-改-写：对 world_state 的**深拷贝**调用 fn(ws) 就地改，再整体重赋值 + 盖版本号 + commit。

    深拷贝确保「改嵌套值」也一定被 ORM 视为变化而落库，从根上规避「改旧值判无变化」的坑。"""
    ws = copy.deepcopy(dict(session.world_state or {}))
    fn(ws)
    ws["schema_version"] = SCHEMA_VERSION
    session.world_state = ws
    db.commit()
