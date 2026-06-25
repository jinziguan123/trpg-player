from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.rules.dice import roll


@dataclass
class CheckResult:
    skill_name: str
    skill_value: int
    roll: int
    target: int
    outcome: str  # critical_success | hard_success | success | failure | fumble
    description: str


@dataclass
class DamageResult:
    target_id: str
    damage_dealt: int
    damage_type: str
    remaining_hp: int
    status_change: str | None


class RuleEngine(ABC):
    """规则引擎抽象基类"""

    @abstractmethod
    def get_rule_system_id(self) -> str: ...

    @abstractmethod
    def get_character_schema(self) -> dict:
        """返回角色卡字段定义 (JSON Schema 格式)"""
        ...

    @abstractmethod
    def create_character(self, data: dict) -> dict:
        """根据输入创建角色，自动计算派生属性"""
        ...

    @abstractmethod
    def validate_character(self, character_data: dict) -> tuple[bool, list[str]]: ...

    @abstractmethod
    def resolve_check(
        self, character_data: dict, skill_name: str, difficulty: str = "normal"
    ) -> CheckResult: ...

    @abstractmethod
    def apply_damage(
        self, target_data: dict, damage: int, damage_type: str = "physical"
    ) -> DamageResult: ...

    def roll_dice(self, notation: str):
        return roll(notation)
