"""CoC 7th Edition 规则引擎"""

from app.rules.base import CheckResult, DamageResult, RuleEngine
from app.rules.coc.character import (
    COC_DEFAULT_SKILLS,
    build_default_skills,
    compute_derived,
    roll_attributes,
)
from app.rules.coc.checks import resolve_skill_check, san_check  # noqa: F401


class CoCRuleEngine(RuleEngine):
    def get_rule_system_id(self) -> str:
        return "coc"

    def get_character_schema(self) -> dict:
        return {
            "rule_system": "coc",
            "attributes": [
                {"key": "STR", "name": "力量", "roll": "3d6×5"},
                {"key": "CON", "name": "体质", "roll": "3d6×5"},
                {"key": "SIZ", "name": "体型", "roll": "(2d6+6)×5"},
                {"key": "DEX", "name": "敏捷", "roll": "3d6×5"},
                {"key": "APP", "name": "外貌", "roll": "3d6×5"},
                {"key": "INT", "name": "智力", "roll": "(2d6+6)×5"},
                {"key": "POW", "name": "意志", "roll": "3d6×5"},
                {"key": "EDU", "name": "教育", "roll": "(2d6+6)×5"},
            ],
            "derived": ["HP", "MP", "SAN", "MOV", "伤害加值", "体格", "幸运"],
            "system_specific_fields": ["sanity", "luck", "age", "occupation"],
            "default_skills": COC_DEFAULT_SKILLS,
        }

    def create_character(self, data: dict) -> dict:
        attrs = data.get("base_attributes", {})
        if not attrs:
            attrs = roll_attributes()

        age = data.get("age", 25)
        system_data = compute_derived(attrs, age)

        extra_sd = data.get("system_data", {})
        if isinstance(extra_sd, dict):
            system_data["occupation"] = extra_sd.get("occupation", "")

        skills = data.get("skills")
        if not skills:
            skills = build_default_skills(attrs.get("EDU", 50))

        return {
            "base_attributes": attrs,
            "skills": skills,
            "system_data": system_data,
        }

    def validate_character(self, character_data: dict) -> tuple[bool, list[str]]:
        errors = []
        attrs = character_data.get("base_attributes", {})
        required = ["STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU"]

        for attr in required:
            val = attrs.get(attr)
            if val is None:
                errors.append(f"缺少属性: {attr}")
            elif not (15 <= val <= 90):
                errors.append(f"{attr} 值 {val} 超出合理范围 (15-90)")

        return (len(errors) == 0, errors)

    def resolve_check(
        self, character_data: dict, skill_name: str, difficulty: str = "normal"
    ) -> CheckResult:
        return resolve_skill_check(character_data, skill_name, difficulty)

    def apply_damage(
        self, target_data: dict, damage: int, damage_type: str = "physical"
    ) -> DamageResult:
        system_data = target_data.get("system_data", {})
        hp = system_data.get("hitPoints", {})
        current_hp = hp.get("current", 0)
        max_hp = hp.get("max", 0)

        new_hp = max(0, current_hp - damage)

        status_change = None
        if new_hp <= 0:
            status_change = "dead"
        elif damage >= max_hp // 2:
            status_change = "major_wound"

        return DamageResult(
            target_id=target_data.get("id", ""),
            damage_dealt=damage,
            damage_type=damage_type,
            remaining_hp=new_hp,
            status_change=status_change,
        )

    def roll_attribute_sets(self, count: int = 3) -> list[dict[str, int]]:
        """掷多组属性供玩家选择"""
        return [roll_attributes() for _ in range(count)]
