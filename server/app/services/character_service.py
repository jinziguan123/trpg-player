from sqlalchemy.orm import Session

import app.rules  # noqa: F401
from app.models.character import Character
from app.rules.registry import get_engine


def roll_attribute_sets(rule_system: str, count: int = 3) -> list[dict[str, int]]:
    engine = get_engine(rule_system)
    return engine.roll_attribute_sets(count)


def get_character_schema(rule_system: str) -> dict:
    engine = get_engine(rule_system)
    return engine.get_character_schema()


def create_character(db: Session, data: dict) -> Character:
    rule_system = data["rule_system"]
    engine = get_engine(rule_system)

    computed = engine.create_character(data)

    ok, errors = engine.validate_character(computed)
    if not ok:
        raise ValueError(f"角色数据校验失败: {', '.join(errors)}")

    character = Character(
        name=data["name"],
        module_id=data.get("module_id"),
        rule_system=rule_system,
        is_player=data.get("is_player", True),
        owner_token=data.get("owner_token"),
        base_attributes=computed["base_attributes"],
        skills=computed["skills"],
        system_data=computed["system_data"],
        backstory=data.get("backstory", ""),
    )
    db.add(character)
    db.commit()
    db.refresh(character)
    return character


def get_character(db: Session, character_id: str) -> Character | None:
    return db.get(Character, character_id)


def list_characters(db: Session, module_id: str | None = None) -> list[Character]:
    q = db.query(Character)
    if module_id:
        q = q.filter(Character.module_id == module_id)
    return q.order_by(Character.created_at.desc()).all()


def delete_character(db: Session, character_id: str) -> bool:
    char = db.get(Character, character_id)
    if not char:
        return False
    db.delete(char)
    db.commit()
    return True


def update_character(db: Session, character_id: str, updates: dict) -> Character | None:
    char = db.get(Character, character_id)
    if not char:
        return None
    for key, val in updates.items():
        if val is not None and hasattr(char, key):
            setattr(char, key, val)
    db.commit()
    db.refresh(char)
    return char
