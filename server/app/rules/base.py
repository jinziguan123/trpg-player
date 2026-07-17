from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.rules.dice import roll


@dataclass
class CheckResult:
    skill_name: str
    skill_value: int
    roll: int
    target: int
    outcome: str  # critical_success | hard_success | success | failure | fumble（相对要求难度的成败，向后兼容）
    description: str
    # 「达成等级」：纯按骰值 vs 技能值算出的六档，与「要求难度」无关——信息量按它分层（req 2/3）。
    tier: str = "regular"  # critical | extreme | hard | regular | fail | fumble
    meets_difficulty: bool = True  # 是否达到本次「要求难度」的及格线（动作成败用）
    # d100 逐骰明细（供前端 3D 骰子动画严格还原）：无奖惩时 tens 只含 1 个、tens_kept==tens[0]。
    # roll 仍是最终 d100（= tens_kept + units，十位00+个位0 视作 100），既有字段/行为不变。
    tens: list[int] = field(default_factory=list)  # 所有掷出的十位（0/10/…/90）
    tens_kept: int = 0     # 最终采用的十位
    units: int = 0         # 个位骰（0-9）
    bonus: int = 0         # 奖励骰数量
    penalty: int = 0       # 惩罚骰数量


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
        self, character_data: dict, skill_name: str, difficulty: str = "normal",
        bonus: int = 0, penalty: int = 0,
    ) -> CheckResult: ...

    # 伤害/重伤/濒死/死亡结算见 app.rules.coc.combat.resolve_wound（不走 engine，避免规则漂移）。

    def roll_dice(self, notation: str):
        return roll(notation)

    def improvement_check(self, current_value: int) -> dict | None:
        """技能成长检定（战后结算）。默认规则系统不支持成长，返回 None；
        支持的引擎（如 CoC）覆盖此方法，返回
        ``{"roll", "improved", "gain", "old_value", "new_value"}``。"""
        return None
