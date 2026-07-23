"""裁判模型：对一段 KP 旁白按 rubric 逐项 0/1 打分。

与确定性检查互补：这里评的是语义层面（泄密暗示、计划遵循、替玩家行动、
出戏、衔接），机器规则查不出来的部分。fail-open：调用/解析失败返回 None，
运行器把该 fixture 标记为 judge_error 而不判失败。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai.turn_planner import TurnPlan

from evals.common import ReplayCase

logger = logging.getLogger(__name__)

RUBRIC = {
    "no_leak": "未泄露安全约束（do_not_reveal）中的隐藏信息，包括转述、总结、暗示",
    "plan_adherence": "遵循裁定计划：该发起的检定发起了（requires_check 时以 [DICE_CHECK] 收尾）、"
                      "线索按 reveal_level 揭示、未越过 clue_policy/npc_policy 的边界；无计划则只看是否自洽",
    "no_player_control": "未替任何玩家角色**新增**行动、说话、心理描写或决定。注意豁免："
                         "玩家本轮已明确宣言的动作，KP 对其做过程性展开（把「我敲击侧板找暗格」"
                         "渲染成俯身、叩击、摸索的画面）**不算违规**——那是检定前的正当铺陈；"
                         "违规是 KP 让玩家做了没宣言过的事、说了没说过的话、下了没下过的判断",
    "in_character": "始终是沉浸式叙事：无汇报体总结、无系统性/技术性语言、无跳出 KP 身份的元评论"
                    "（方括号指令是系统语法，不算违规）",
    "coherence": "与最近事件自然衔接：回应了玩家本轮的行动/发言，没有无视输入或凭空跳转",
    "subject_fidelity": "叙述主语归属正确：每个动作/所见/所得/所悟都落到正确的角色名下，没有张冠李戴。"
                        "尤其当最近事件里有某角色的检定结果时，该结果**只能**叙述成那名执行者的所得，"
                        "**绝不能安到别的角色头上**（如把甲的智力检定结果写成乙在推理/观察）。"
                        "只有一名玩家角色、或无检定归属歧义时，本项默认通过",
    "perception_isolation": "NPC 感知边界：每个 NPC 只对「本人在场目睹/听到、被当面告知、或隔墙可闻的巨响"
                            "（枪声/尖叫级）」的事作出反应。若上下文表明人员分处不同地点（不同场景，或叙事"
                            "确立的门/墙/楼层之隔），NPC 对别处发生的言行**不得**评论、追问、神色变化或表现出"
                            "任何知情。无 NPC 出场、或全员同处一地时，本项默认通过",
    "improvised_containment": "临场角色收容：模组未列出、KP 临时添加的龙套（见上下文「临场角色名单」）"
                              "**不得携带或产出线索、秘密、关键情报，不得把守剧情、成为唯一知情人**。"
                              "玩家追问时龙套如实不知、至多指回模组内容（模组 NPC/场景），不得现编往事或情报"
                              "来满足追问。无临场角色出场时本项默认通过",
    "combat_engine_authority": "战斗/追逐叙述只据引擎结算续写：**不自报具体伤害数字或骰点**、不臆造未发生的"
                               "命中/闪避/倒下、不替玩家决定攻击目标或防御方式；已倒下（昏迷/濒死/死亡/逃离）的"
                               "角色不再行动。非战斗轮次本项默认通过",
    "combat_turn_order": "结构化战斗轮里按先攻顺序推进、每人每轮一个主要动作，不让同一角色一轮内重复行动、"
                         "不越过轮到的行动者。非战斗轮次本项默认通过",
}

# 正向观测项：量化叙事质量走势（场景感/节奏），随 --repeat 看通过率与方差。
# **不参与 fixture 通过判定**——主观维度做门禁会让基线抖动；只记录、只对比趋势。
# 评判倾向与防守项相反：仅在缺陷明显时判不通过，拿不准就通过。
ADVISORY_RUBRIC = {
    "scene_texture": "【观测】场景感：旁白让当前场景可感——至少有一处具体的环境/感官细节"
                     "（视觉、声响、气味、光影、空间关系或物件质感），且与既有场景设定一致。"
                     "只在通篇是干瘪的事件陈述、或细节与既有描述矛盾时判不通过",
    "pacing": "【观测】节奏：篇幅与信息密度同本轮事件的分量相称——过场小事不铺陈成大段、"
              "关键揭示不一笔带过；没有原地打转的重复描写或凑字的空话；结尾收得干净"
              "（收在检定、提问或留白上），不拖泥带水。只在明显失衡时判不通过",
}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _recent_events_text(case: ReplayCase, limit: int = 12) -> str:
    lines = []
    for e in case.events[-limit:]:
        lines.append(f"[{e.event_type}] {e.actor_name or '系统'}: {(e.content or '')[:200]}")
    return "\n".join(lines) or "（无历史事件，本轮为开场）"


def build_judge_messages(
    case: ReplayCase, plan: TurnPlan | None, narration: str,
) -> list[dict]:
    plan_text = (
        json.dumps(plan.model_dump(), ensure_ascii=False) if plan else "（本轮无裁定计划）"
    )
    all_rubric = {**RUBRIC, **ADVISORY_RUBRIC}
    rubric_text = "\n".join(f"- {key}: {desc}" for key, desc in all_rubric.items())
    schema = ", ".join(f'"{k}": {{"pass": true, "reason": ""}}' for k in all_rubric)
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 跑团质量评审，对 AI 守秘人（KP）生成的一段旁白逐项打分。"
                "严格按事实评判，不确定时倾向判不通过；"
                "但带【观测】标注的正向项倾向相反：仅在缺陷明显时判不通过，拿不准就通过。"
                "只输出一个 JSON object，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"玩家角色（KP 绝不可替他们行动/说话）：{', '.join(case.player_names)}\n\n"
                f"本轮裁定计划（KP 应遵循的约束）：\n{plan_text}\n\n"
                f"最近事件（旁白应与之衔接）：\n{_recent_events_text(case)}\n\n"
                f"待评审的 KP 旁白：\n{narration}\n\n"
                f"评分项定义：\n{rubric_text}\n\n"
                f'对每项给出 pass（bool）与 reason（不通过时一句话说明，通过留空），'
                f"返回 {{{schema}}}。只输出 JSON。"
            ),
        },
    ]


def _parse_judge_output(raw: str) -> dict[str, dict] | None:
    text = raw.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    result = {}
    for key in RUBRIC:
        item = data.get(key)
        if not isinstance(item, dict) or "pass" not in item:
            return None  # 缺项视为解析失败，宁可 judge_error 也不给假分
        result[key] = {"pass": bool(item["pass"]), "reason": str(item.get("reason") or "")}
    for key in ADVISORY_RUBRIC:
        item = data.get(key)
        if not isinstance(item, dict) or "pass" not in item:
            continue  # 观测项缺失不构成 judge_error（不参与通过判定，宁缺毋假）
        result[key] = {"pass": bool(item["pass"]), "reason": str(item.get("reason") or "")}
    return result


async def run_judge(
    llm: Any, case: ReplayCase, plan: TurnPlan | None, narration: str,
) -> dict[str, dict] | None:
    """返回 {rubric_key: {"pass": bool, "reason": str}}；失败返回 None。"""
    try:
        raw = await llm.complete(
            build_judge_messages(case, plan, narration),
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("judge 调用失败: fixture=%s", case.name)
        return None
    parsed = _parse_judge_output(raw)
    if parsed is None:
        logger.warning("judge 输出无法解析: fixture=%s raw=%.200s", case.name, raw)
    return parsed
