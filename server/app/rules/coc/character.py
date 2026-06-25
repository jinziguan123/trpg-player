"""CoC 7th Edition 角色创建逻辑"""

from app.rules.dice import roll

# CoC 7th 基础属性掷骰规则
ATTRIBUTE_ROLLS = {
    "STR": "3d6",   # 力量
    "CON": "3d6",   # 体质
    "SIZ": "2d6",   # 体型 (2d6+6)*5
    "DEX": "3d6",   # 敏捷
    "APP": "3d6",   # 外貌
    "INT": "2d6",   # 智力 (2d6+6)*5
    "POW": "3d6",   # 意志
    "EDU": "2d6",   # 教育 (2d6+6)*5
}

# 2d6+6 的属性列表
PLUS_SIX_ATTRS = {"SIZ", "INT", "EDU"}

COC_DEFAULT_SKILLS: dict[str, int] = {
    "会计": 5, "人类学": 1, "估价": 5, "考古学": 1, "魅惑": 15,
    "攀爬": 20, "计算机使用": 5, "信用评级": 0, "克苏鲁神话": 0,
    "乔装": 5, "闪避": 0, "驾驶": 20, "电气维修": 10, "电子学": 1,
    "话术": 5, "格斗(斗殴)": 25, "射击(手枪)": 20, "射击(步枪)": 25,
    "急救": 30, "历史": 5, "恐吓": 15, "跳跃": 20, "母语": 0,
    "法律": 5, "图书馆使用": 20, "聆听": 20, "锁匠": 1, "机械维修": 10,
    "医学": 1, "博物学": 10, "导航": 10, "神秘学": 5, "操作重型机械": 1,
    "说服": 10, "摄影": 5, "精神分析": 1, "心理学": 10, "骑术": 5,
    "科学": 1, "妙手": 10, "侦查": 25, "潜行": 20, "游泳": 20,
    "投掷": 20, "追踪": 10, "驯兽": 5,
}


def roll_attributes() -> dict[str, int]:
    """掷一组 CoC 基础属性"""
    attrs = {}
    for name, notation in ATTRIBUTE_ROLLS.items():
        result = roll(notation)
        if name in PLUS_SIX_ATTRS:
            attrs[name] = (result.total + 6) * 5
        else:
            attrs[name] = result.total * 5
    return attrs


def compute_derived(attrs: dict[str, int], age: int = 25) -> dict:
    """计算派生属性"""
    str_val = attrs.get("STR", 50)
    con_val = attrs.get("CON", 50)
    siz_val = attrs.get("SIZ", 50)
    dex_val = attrs.get("DEX", 50)
    pow_val = attrs.get("POW", 50)
    edu_val = attrs.get("EDU", 50)

    hp = (con_val + siz_val) // 10
    mp = pow_val // 5
    san = pow_val
    luck = roll("3d6").total * 5

    # 移动力
    if dex_val < siz_val and str_val < siz_val:
        mov = 7
    elif dex_val >= siz_val or str_val >= siz_val:
        mov = 8
    else:
        mov = 9

    if age >= 80:
        mov -= 5
    elif age >= 70:
        mov -= 4
    elif age >= 60:
        mov -= 3
    elif age >= 50:
        mov -= 2
    elif age >= 40:
        mov -= 1

    # 伤害加值和体格
    combined = str_val + siz_val
    if combined <= 64:
        db, build = "-2", -2
    elif combined <= 84:
        db, build = "-1", -1
    elif combined <= 124:
        db, build = "0", 0
    elif combined <= 164:
        db, build = "1d4", 1
    else:
        db, build = "1d6", 2

    return {
        "hitPoints": {"current": hp, "max": hp},
        "magicPoints": {"current": mp, "max": mp},
        "sanity": {"current": san, "max": 99},
        "luck": luck,
        "move": mov,
        "damageBonus": db,
        "build": build,
        "age": age,
        "occupation": "",
    }


def build_default_skills(edu: int) -> dict[str, int]:
    """基于 EDU 构建默认技能列表（含母语和闪避）"""
    skills = dict(COC_DEFAULT_SKILLS)
    skills["母语"] = edu
    skills["闪避"] = skills.get("DEX", 50) // 2
    return skills
