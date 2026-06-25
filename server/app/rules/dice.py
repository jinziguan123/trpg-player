import random
import re
from dataclasses import dataclass, field


@dataclass
class DiceRollResult:
    notation: str
    rolls: list[int] = field(default_factory=list)
    modifier: int = 0
    total: int = 0


def roll(notation: str) -> DiceRollResult:
    """解析并执行骰子表达式，如 '2d6+3', '1d100', '3d6'"""
    match = re.match(r"(\d+)d(\d+)([+-]\d+)?", notation.strip().lower())
    if not match:
        raise ValueError(f"无效的骰子表达式: {notation}")

    count = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier

    return DiceRollResult(
        notation=notation,
        rolls=rolls,
        modifier=modifier,
        total=total,
    )


def roll_percentile() -> int:
    return random.randint(1, 100)
