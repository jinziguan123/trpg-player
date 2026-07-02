"""fixture 的序列化/反序列化与重放用例重建。

fixture = 截至某轮的完整重放材料：模组、会话、角色、事件流。
重放时把这些 dict 重建成**脱库（detached）的 ORM 实例**——build_kp_context /
turn_planner 只做属性访问，不触数据库，因此无需临时库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect as sa_inspect

from app.ai.turn_planner import TurnPlan
from app.models import Character, EventLog, GameSession, Module

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

_DATETIME_KEYS = ("created_at", "updated_at")


def row_to_dict(obj: Any) -> dict:
    """ORM 实例 → 纯 dict（按映射属性名，datetime 转 isoformat）。"""
    data: dict[str, Any] = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        value = getattr(obj, attr.key)
        if isinstance(value, datetime):
            value = value.isoformat()
        data[attr.key] = value
    return data


def dict_to_model(model_cls: type, data: dict) -> Any:
    """dict → 脱库 ORM 实例。只取该模型已映射的属性，忽略多余键（向后兼容旧 fixture）。"""
    valid_keys = {attr.key for attr in sa_inspect(model_cls).mapper.column_attrs}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in valid_keys:
            continue
        if key in _DATETIME_KEYS and isinstance(value, str):
            value = datetime.fromisoformat(value)
        kwargs[key] = value
    return model_cls(**kwargs)


@dataclass
class ReplayCase:
    """一个可重放的评测用例。"""

    name: str
    session: GameSession
    module: Module
    player_char: Character
    teammates: list[Character]
    events: list[EventLog]
    rules_lookup_enabled: bool = False
    plan: TurnPlan | None = None  # fixture 预存的裁定计划；None 则重放时现跑 planner
    tags: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def player_names(self) -> list[str]:
        return [self.player_char.name] + [t.name for t in self.teammates]


def save_fixture(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_fixture(path: Path) -> ReplayCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    plan_data = payload.get("plan")
    events = [dict_to_model(EventLog, e) for e in payload.get("events") or []]
    events.sort(key=lambda e: e.sequence_num or 0)
    return ReplayCase(
        name=meta.get("name") or path.stem,
        session=dict_to_model(GameSession, payload["session"]),
        module=dict_to_model(Module, payload["module"]),
        player_char=dict_to_model(Character, payload["player_char"]),
        teammates=[dict_to_model(Character, t) for t in payload.get("teammates") or []],
        events=events,
        rules_lookup_enabled=bool(payload.get("rules_lookup_enabled")),
        plan=TurnPlan.model_validate(plan_data) if plan_data else None,
        tags=list(meta.get("tags") or []),
        note=meta.get("note") or "",
    )


def iter_fixtures(suite: str | None = None, name: str | None = None) -> list[Path]:
    """按 --suite（tag 过滤）/ --fixture（单个名字）列出 fixture 文件。"""
    paths = sorted(FIXTURES_DIR.glob("*.json"))
    if name:
        return [p for p in paths if p.stem == name]
    if not suite:
        return paths
    picked = []
    for p in paths:
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("meta") or {}
        except (json.JSONDecodeError, OSError):
            continue
        if suite in (meta.get("tags") or []):
            picked.append(p)
    return picked
