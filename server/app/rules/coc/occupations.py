"""CoC 7th Edition 职业列表及技能点计算"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Occupation:
    name: str
    credit_min: int
    credit_max: int
    skill_formula: str
    skills: list[str]
    choices: int = 0  # 可自选技能数


def _calc_points(formula: str, attrs: dict[str, int]) -> int:
    """解析公式如 'EDU*4', 'EDU*2+DEX*2', 'EDU*2+STR|DEX*2'"""
    import re

    attr_map = {
        "EDU": attrs.get("EDU", 0),
        "STR": attrs.get("STR", 0),
        "DEX": attrs.get("DEX", 0),
        "APP": attrs.get("APP", 0),
        "POW": attrs.get("POW", 0),
        "INT": attrs.get("INT", 0),
        "CON": attrs.get("CON", 0),
        "SIZ": attrs.get("SIZ", 0),
    }
    parts = formula.split("+")
    total = 0
    for part in parts:
        part = part.strip()
        if "|" in part:
            # "STR|DEX*2" → max(STR, DEX) * 2
            m = re.match(r"([\w|]+)\*(\d+)", part)
            if m:
                attr_names = m.group(1).split("|")
                mult = int(m.group(2))
                total += max(attr_map.get(a.strip(), 0) for a in attr_names) * mult
        else:
            attr, mult = part.split("*")
            total += attr_map.get(attr.strip(), 0) * int(mult)
    return total


def calc_occupation_points(occupation_name: str, attrs: dict[str, int]) -> int:
    occ = get_occupation(occupation_name)
    if not occ:
        return attrs.get("EDU", 0) * 4
    return _calc_points(occ.skill_formula, attrs)


def calc_interest_points(attrs: dict[str, int]) -> int:
    return attrs.get("INT", 0) * 2


def get_occupation(name: str) -> Occupation | None:
    for occ in COC_OCCUPATIONS:
        if occ.name == name:
            return occ
    return None


COC_OCCUPATIONS: list[Occupation] = [
    Occupation(
        name="会计师",
        credit_min=30, credit_max=70,
        skill_formula="EDU*4",
        skills=["会计", "法律", "图书馆使用", "聆听", "说服", "侦查"],
        choices=2,
    ),
    Occupation(
        name="演员",
        credit_min=9, credit_max=40,
        skill_formula="EDU*2+APP*2",
        skills=["乔装", "历史", "心理学"],
        choices=2,
    ),
    Occupation(
        name="事务所侦探",
        credit_min=20, credit_max=45,
        skill_formula="EDU*2+STR|DEX*2",
        skills=["格斗(斗殴)", "射击(手枪)", "法律", "图书馆使用", "心理学", "潜行", "追踪"],
        choices=1,
    ),
    Occupation(
        name="艺术家",
        credit_min=9, credit_max=50,
        skill_formula="EDU*2+DEX|POW*2",
        skills=["历史", "心理学", "侦查"],
        choices=2,
    ),
    Occupation(
        name="运动员",
        credit_min=9, credit_max=70,
        skill_formula="EDU*2+DEX|STR*2",
        skills=["攀爬", "跳跃", "格斗(斗殴)", "骑术", "游泳", "投掷"],
        choices=1,
    ),
    Occupation(
        name="作家",
        credit_min=9, credit_max=30,
        skill_formula="EDU*4",
        skills=["历史", "图书馆使用", "母语", "神秘学", "心理学"],
        choices=1,
    ),
    Occupation(
        name="酒保",
        credit_min=8, credit_max=25,
        skill_formula="EDU*2+APP*2",
        skills=["会计", "格斗(斗殴)", "聆听", "心理学", "侦查"],
        choices=1,
    ),
    Occupation(
        name="神职人员",
        credit_min=9, credit_max=60,
        skill_formula="EDU*4",
        skills=["会计", "历史", "图书馆使用", "聆听", "心理学"],
        choices=1,
    ),
    Occupation(
        name="医生",
        credit_min=30, credit_max=80,
        skill_formula="EDU*4",
        skills=["急救", "医学", "心理学", "聆听", "说服", "侦查"],
        choices=2,
    ),
    Occupation(
        name="工程师",
        credit_min=30, credit_max=60,
        skill_formula="EDU*4",
        skills=["电气维修", "图书馆使用", "机械维修", "侦查"],
        choices=2,
    ),
    Occupation(
        name="记者",
        credit_min=9, credit_max=30,
        skill_formula="EDU*4",
        skills=["历史", "图书馆使用", "母语", "心理学", "侦查"],
        choices=1,
    ),
    Occupation(
        name="律师",
        credit_min=30, credit_max=80,
        skill_formula="EDU*4",
        skills=["会计", "法律", "图书馆使用", "说服", "心理学", "侦查"],
        choices=2,
    ),
    Occupation(
        name="警察",
        credit_min=9, credit_max=30,
        skill_formula="EDU*2+DEX|STR*2",
        skills=["格斗(斗殴)", "射击(手枪)", "急救", "法律", "心理学", "侦查", "追踪"],
        choices=1,
    ),
    Occupation(
        name="教授",
        credit_min=20, credit_max=70,
        skill_formula="EDU*4",
        skills=["图书馆使用", "母语", "心理学"],
        choices=2,
    ),
    Occupation(
        name="军官",
        credit_min=20, credit_max=70,
        skill_formula="EDU*2+DEX|STR*2",
        skills=["格斗(斗殴)", "射击(手枪)", "导航", "心理学", "侦查"],
        choices=2,
    ),
    Occupation(
        name="考古学家",
        credit_min=10, credit_max=40,
        skill_formula="EDU*4",
        skills=["估价", "考古学", "历史", "图书馆使用", "侦查", "机械维修", "导航"],
        choices=1,
    ),
    Occupation(
        name="程序员",
        credit_min=10, credit_max=70,
        skill_formula="EDU*4",
        skills=["计算机使用", "电气维修", "电子学", "图书馆使用", "侦查"],
        choices=2,
    ),
    Occupation(
        name="窃贼",
        credit_min=5, credit_max=40,
        skill_formula="EDU*2+DEX*2",
        skills=["估价", "攀爬", "聆听", "锁匠", "妙手", "潜行", "侦查"],
        choices=1,
    ),
    Occupation(
        name="猎人",
        credit_min=20, credit_max=50,
        skill_formula="EDU*2+DEX|STR*2",
        skills=["射击(步枪)", "聆听", "博物学", "导航", "潜行", "追踪"],
        choices=2,
    ),
    Occupation(
        name="古董商",
        credit_min=30, credit_max=50,
        skill_formula="EDU*4",
        skills=["会计", "估价", "驾驶", "历史", "图书馆使用", "导航"],
        choices=2,
    ),
]
