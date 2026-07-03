"""成长结算（P4 5.2c）：会话内成功使用过的技能，战后按规则引擎做成长检定并落库。

数据源确定性：dice 事件的 metadata（skill / outcome / actor）。规则逻辑走 RuleEngine
的 improvement_check（插件式，不硬编码 CoC）。不支持成长的规则系统自然降级为空结果。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import session_service

# 达成等级/结果里代表「成功」的取值——只要成功用过一次即获得该技能的成长机会。
_SUCCESS_TIERS = {"critical", "extreme", "hard", "regular"}
_SUCCESS_OUTCOMES = {"critical_success", "hard_success", "success"}


def _is_success(md: dict) -> bool:
    return (md.get("tier") in _SUCCESS_TIERS) or (str(md.get("outcome") or "") in _SUCCESS_OUTCOMES)


def eligible_skills(db: Session, session_id: str, character_id: str) -> list[dict]:
    """该角色本局成功用过、且在其技能表内的技能（排除属性/SAN 等非技能检定）。"""
    char = db.get(Character, character_id)
    if char is None:
        return []
    skills = char.skills or {}
    events = session_service.get_session_events(db, session_id, limit=0)
    used: set[str] = set()
    for e in events:
        if getattr(e, "event_type", None) != "dice":
            continue
        md = e.metadata_ or {}
        skill = md.get("skill")
        if not skill or skill not in skills:
            continue
        if md.get("actor") and md.get("actor") != char.name:
            continue
        if _is_success(md):
            used.add(skill)
    return [{"skill": s, "value": skills[s]} for s in sorted(used)]


def settle_growth(db: Session, session_id: str, character_id: str) -> dict | None:
    """对全部可成长技能逐项做成长检定并把成长应用到角色技能表；返回逐项结果。

    会话/角色/模组缺失返回 None。规则系统不支持成长时 results 为空（各项 improvement_check
    返回 None 被跳过）。掷骰在服务端权威进行，前端可据结果做逐项揭示动画。
    """
    session = db.get(GameSession, session_id)
    char = db.get(Character, character_id)
    if session is None or char is None:
        return None
    module = db.get(Module, session.module_id) if session.module_id else None
    if module is None:
        return None
    engine = get_engine(module.rule_system)

    skills = dict(char.skills or {})
    results: list[dict] = []
    for item in eligible_skills(db, session_id, character_id):
        s = item["skill"]
        current = skills.get(s, item["value"])
        res = engine.improvement_check(current)
        if res is None:
            continue  # 该规则系统不支持成长
        if res.get("improved") and res.get("new_value", current) > current:
            skills[s] = res["new_value"]
        results.append({"skill": s, **res})

    char.skills = skills  # JSON 列整体重赋值才会被标脏
    db.commit()
    return {"character_id": character_id, "character_name": char.name, "results": results}
