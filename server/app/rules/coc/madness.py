"""CoC 临时疯狂『一阵疯狂』症状表与影响判定（参照 7e『Bout of Madness — Summary』改编）。

进入临时疯狂时掷 1D10 随机一种症状：症状对**检定**（惩罚骰）与**言行**（叙事表现，
incapacitated 者发作期间无法正常自主行动、由系统/KP 接管）产生影响，持续 1D10 个回合后自动解除。
"""

from __future__ import annotations

import random

# 每种症状：key（稳定标识）/ label（中文）/ manifest（一句话表现，注入叙事与播报）/
# penalty_skills（命中这些技能域的检定加一颗惩罚骰）/ incapacitated（发作期间无法正常自主行动=
# 系统接管，由 KP 叙述其不自主的疯狂行为）/ override（接管时的强制行为：violence/faint/flee）。
BOUT_SYMPTOMS: list[dict] = [
    {"key": "amnesia", "label": "失忆",
     "manifest": "对刚刚发生的一切一片空白，茫然地反复追问自己身在何处、方才发生了什么",
     "penalty_skills": ["灵感", "知识", "教育", "历史", "回忆"], "incapacitated": False, "override": ""},
    {"key": "blindness", "label": "癔症性失明",
     "manifest": "眼前骤然陷入漆黑，什么也看不见，只能伸手摸索",
     "penalty_skills": ["侦查", "观察", "图书馆使用", "阅读", "追踪", "侦察"],
     "incapacitated": False, "override": ""},
    {"key": "deafness", "label": "癔症性失聪",
     "manifest": "世界骤然沉入死寂，听不见任何声音，答非所问",
     "penalty_skills": ["聆听"], "incapacitated": False, "override": ""},
    {"key": "violence", "label": "暴力冲动",
     "manifest": "被无法遏制的暴力冲动攫住，对最近的目标（甚至同伴）发起攻击",
     "penalty_skills": [], "incapacitated": True, "override": "violence"},
    {"key": "paranoia", "label": "偏执妄想",
     "manifest": "疑神疑鬼，坚信身边有人（包括同伴）正密谋加害自己，拒绝信任与配合",
     "penalty_skills": ["话术", "取悦", "魅惑", "说服", "社交", "信用评级"],
     "incapacitated": False, "override": ""},
    {"key": "significant_person", "label": "认错至亲",
     "manifest": "把在场某人错认成生命中挥之不去的重要之人，纠缠不休、情绪失控",
     "penalty_skills": ["话术", "侦查", "灵感"], "incapacitated": False, "override": ""},
    {"key": "faint", "label": "昏厥",
     "manifest": "承受不住冲击，当场两眼一黑、瘫软昏厥倒地",
     "penalty_skills": [], "incapacitated": True, "override": "faint"},
    {"key": "flee", "label": "惊恐逃窜",
     "manifest": "彻底被恐惧支配，尖叫着不顾一切地夺路逃离现场，无视危险",
     "penalty_skills": [], "incapacitated": True, "override": "flee"},
    {"key": "hysteria", "label": "歇斯底里",
     "manifest": "情绪彻底失控，无法自抑地嚎啕大哭 / 狂笑 / 浑身剧烈颤抖",
     "penalty_skills": ["意志", "话术", "取悦", "社交"], "incapacitated": False, "override": ""},
    {"key": "phobia_mania", "label": "恐惧发作",
     "manifest": "一种突如其来的强烈恐惧攫住理智，本能地回避、退缩、语无伦次",
     "penalty_skills": ["意志"], "incapacitated": False, "override": ""},
]


def roll_symptom() -> dict:
    """掷 1D10 随机一种一阵疯狂症状（返回症状定义的浅拷贝）。"""
    return dict(BOUT_SYMPTOMS[random.randint(0, len(BOUT_SYMPTOMS) - 1)])


def roll_duration() -> int:
    """一阵疯狂持续回合数：1D10。"""
    return random.randint(1, 10)


def make_bout() -> dict:
    """生成一次发作的状态对象，存进 char.system_data['madness']；turns_left 到 0 自动解除。"""
    s = roll_symptom()
    return {
        "symptom": s["key"], "label": s["label"], "manifest": s["manifest"],
        "penalty_skills": s["penalty_skills"], "incapacitated": s["incapacitated"],
        "override": s["override"], "turns_left": roll_duration(),
    }


def check_penalty(madness: dict | None, skill: str) -> int:
    """该症状是否给这次检定加惩罚骰：技能名命中 penalty_skills 任一即 +1，否则 0。"""
    if not madness or not skill:
        return 0
    s = skill.strip()
    for dom in madness.get("penalty_skills") or []:
        if dom and (dom in s or s in dom):
            return 1
    return 0
