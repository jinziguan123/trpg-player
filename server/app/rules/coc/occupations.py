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
    # ===== 原有 20 个 =====
    Occupation("会计师", 30, 70, "EDU*4",
               ["会计", "法律", "图书馆使用", "聆听", "说服", "侦查"], 2),
    Occupation("演员", 9, 40, "EDU*2+APP*2",
               ["乔装", "历史", "心理学"], 2),
    Occupation("事务所侦探", 20, 45, "EDU*2+STR|DEX*2",
               ["格斗(斗殴)", "射击(手枪)", "法律", "图书馆使用", "心理学", "潜行", "追踪"], 1),
    Occupation("艺术家", 9, 50, "EDU*2+DEX|POW*2",
               ["历史", "心理学", "侦查"], 2),
    Occupation("运动员", 9, 70, "EDU*2+DEX|STR*2",
               ["攀爬", "跳跃", "格斗(斗殴)", "骑术", "游泳", "投掷"], 1),
    Occupation("作家", 9, 30, "EDU*4",
               ["历史", "图书馆使用", "母语", "神秘学", "心理学"], 1),
    Occupation("酒保", 8, 25, "EDU*2+APP*2",
               ["会计", "格斗(斗殴)", "聆听", "心理学", "侦查"], 1),
    Occupation("神职人员", 9, 60, "EDU*4",
               ["会计", "历史", "图书馆使用", "聆听", "心理学"], 1),
    Occupation("医生", 30, 80, "EDU*4",
               ["急救", "医学", "心理学", "聆听", "说服", "侦查"], 2),
    Occupation("工程师", 30, 60, "EDU*4",
               ["电气维修", "图书馆使用", "机械维修", "侦查"], 2),
    Occupation("记者", 9, 30, "EDU*4",
               ["历史", "图书馆使用", "母语", "心理学", "侦查"], 1),
    Occupation("律师", 30, 80, "EDU*4",
               ["会计", "法律", "图书馆使用", "说服", "心理学", "侦查"], 2),
    Occupation("警察", 9, 30, "EDU*2+DEX|STR*2",
               ["格斗(斗殴)", "射击(手枪)", "急救", "法律", "心理学", "侦查", "追踪"], 1),
    Occupation("教授", 20, 70, "EDU*4",
               ["图书馆使用", "母语", "心理学"], 2),
    Occupation("军官", 20, 70, "EDU*2+DEX|STR*2",
               ["格斗(斗殴)", "射击(手枪)", "导航", "心理学", "侦查"], 2),
    Occupation("考古学家", 10, 40, "EDU*4",
               ["估价", "考古学", "历史", "图书馆使用", "侦查", "机械维修", "导航"], 1),
    Occupation("程序员", 10, 70, "EDU*4",
               ["计算机使用", "电气维修", "电子学", "图书馆使用", "侦查"], 2),
    Occupation("窃贼", 5, 40, "EDU*2+DEX*2",
               ["估价", "攀爬", "聆听", "锁匠", "妙手", "潜行", "侦查"], 1),
    Occupation("猎人", 20, 50, "EDU*2+DEX|STR*2",
               ["射击(步枪)", "聆听", "博物学", "导航", "潜行", "追踪"], 2),
    Occupation("古董商", 30, 50, "EDU*4",
               ["会计", "估价", "驾驶", "历史", "图书馆使用", "导航"], 2),
    # ===== 新增职业 =====
    Occupation("飞行员", 20, 70, "EDU*2+DEX*2",
               ["电气维修", "机械维修", "导航", "侦查", "驾驶"], 2),
    Occupation("银行家", 40, 90, "EDU*4",
               ["会计", "法律", "说服", "心理学", "侦查"], 2),
    Occupation("书商", 20, 40, "EDU*4",
               ["会计", "估价", "历史", "图书馆使用", "母语", "侦查"], 2),
    Occupation("赏金猎人", 9, 30, "EDU*2+STR|DEX*2",
               ["格斗(斗殴)", "射击(手枪)", "驾驶", "法律", "潜行", "追踪", "侦查"], 1),
    Occupation("拳击手", 9, 60, "EDU*2+STR*2",
               ["格斗(斗殴)", "闪避", "恐吓", "跳跃", "侦查"], 2),
    Occupation("手艺人", 10, 40, "EDU*2+DEX*2",
               ["会计", "机械维修", "博物学", "侦查"], 2),
    Occupation("罪犯", 5, 65, "EDU*2+DEX|STR*2",
               ["格斗(斗殴)", "恐吓", "锁匠", "妙手", "侦查", "潜行"], 1),
    Occupation("流浪汉", 0, 5, "EDU*2+DEX|STR*2",
               ["攀爬", "跳跃", "聆听", "导航", "潜行", "博物学"], 1),
    Occupation("司机", 9, 20, "EDU*2+DEX*2",
               ["会计", "驾驶", "聆听", "机械维修", "导航", "电气维修"], 1),
    Occupation("艺人", 9, 40, "EDU*2+APP*2",
               ["乔装", "聆听", "心理学", "侦查"], 2),
    Occupation("农场主", 9, 30, "EDU*2+DEX|STR*2",
               ["驾驶", "机械维修", "博物学", "追踪", "驯兽"], 2),
    Occupation("消防员", 9, 30, "EDU*2+DEX|STR*2",
               ["攀爬", "急救", "跳跃", "机械维修", "侦查", "投掷"], 1),
    Occupation("法医", 40, 70, "EDU*4",
               ["急救", "法律", "医学", "摄影", "科学", "侦查", "图书馆使用"], 1),
    Occupation("赌徒", 8, 50, "EDU*2+APP|DEX*2",
               ["会计", "聆听", "心理学", "妙手", "侦查", "话术"], 1),
    Occupation("黑帮老大", 60, 95, "EDU*2+STR|APP*2",
               ["格斗(斗殴)", "射击(手枪)", "恐吓", "法律", "心理学", "说服", "侦查"], 1),
    Occupation("黑帮喽啰", 9, 20, "EDU*2+STR|DEX*2",
               ["格斗(斗殴)", "射击(手枪)", "恐吓", "驾驶", "潜行"], 2),
    Occupation("绅士/淑女", 40, 90, "EDU*2+APP*2",
               ["历史", "母语", "骑术", "说服", "侦查"], 2),
    Occupation("传教士", 0, 30, "EDU*4",
               ["急救", "机械维修", "医学", "说服", "母语"], 2),
    Occupation("音乐家", 9, 30, "EDU*2+DEX|POW*2",
               ["聆听", "心理学", "侦查"], 2),
    Occupation("护士", 15, 35, "EDU*4",
               ["急救", "聆听", "医学", "心理学", "说服", "侦查", "科学"], 1),
    Occupation("超心理学家", 9, 30, "EDU*4",
               ["人类学", "历史", "图书馆使用", "神秘学", "摄影", "心理学", "侦查"], 1),
    Occupation("私家侦探", 9, 30, "EDU*2+STR|DEX*2",
               ["会计", "乔装", "法律", "图书馆使用", "心理学", "侦查", "摄影"], 1),
    Occupation("心理医生", 30, 80, "EDU*4",
               ["聆听", "医学", "心理学", "精神分析", "说服", "科学"], 1),
    Occupation("研究员", 9, 30, "EDU*4",
               ["图书馆使用", "母语", "历史", "侦查"], 2),
    Occupation("水手", 9, 30, "EDU*2+DEX|STR*2",
               ["急救", "格斗(斗殴)", "机械维修", "导航", "游泳", "投掷"], 1),
    Occupation("科学家", 9, 50, "EDU*4",
               ["计算机使用", "图书馆使用", "科学", "侦查"], 2),
    Occupation("士兵", 9, 30, "EDU*2+DEX|STR*2",
               ["格斗(斗殴)", "射击(步枪)", "急救", "潜行", "侦查"], 2),
    Occupation("间谍", 20, 60, "EDU*2+APP|DEX*2",
               ["乔装", "射击(手枪)", "聆听", "心理学", "妙手", "潜行", "侦查"], 1),
    Occupation("部落成员", 0, 15, "EDU*2+STR|DEX*2",
               ["攀爬", "格斗(斗殴)", "博物学", "聆听", "追踪", "游泳", "投掷"], 1),
    Occupation("服务员", 9, 20, "EDU*2+APP*2",
               ["会计", "聆听", "心理学", "侦查", "闪避"], 1),
    Occupation("管家", 9, 40, "EDU*4",
               ["会计", "估价", "急救", "聆听", "心理学", "侦查"], 2),
    Occupation("外交官", 20, 70, "EDU*2+APP*2",
               ["历史", "法律", "母语", "说服", "心理学", "侦查"], 2),
    Occupation("药剂师", 35, 75, "EDU*4",
               ["会计", "急救", "科学", "说服", "图书馆使用", "心理学", "侦查"], 1),
    Occupation("兽医", 9, 40, "EDU*4",
               ["急救", "医学", "博物学", "驯兽", "科学", "侦查"], 2),
    Occupation("图书管理员", 9, 35, "EDU*4",
               ["会计", "历史", "图书馆使用", "母语", "侦查"], 2),
    Occupation("博物馆馆长", 10, 30, "EDU*4",
               ["会计", "估价", "考古学", "历史", "图书馆使用", "侦查"], 2),
    Occupation("摄影师", 9, 30, "EDU*4",
               ["摄影", "心理学", "科学", "侦查", "说服"], 2),
    Occupation("出租车司机", 9, 20, "EDU*2+DEX*2",
               ["驾驶", "机械维修", "导航", "聆听", "侦查", "电气维修"], 1),
    Occupation("动物训练师", 10, 25, "EDU*2+APP|POW*2",
               ["跳跃", "聆听", "博物学", "驯兽", "骑术", "追踪"], 1),
]
