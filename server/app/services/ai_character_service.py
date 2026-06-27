"""AI 自动生成 CoC 七版角色卡

设计原则：规则引擎保证合法（掷属性、算点数、技能钳制），LLM 保证贴合模组
与有趣（选职业、技能加点意图、姓名/背景）。返回结构与
``excel_import.parse_coc_character_sheet`` 对齐，供前端复用同一套填表逻辑。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.ai.deepseek import get_llm
from app.models.module import Module
from app.rules.coc.character import (
    COC_DEFAULT_SKILLS,
    build_default_skills,
    compute_derived,
    roll_attributes,
)
from app.rules.coc.occupations import (
    COC_OCCUPATIONS,
    Occupation,
    calc_interest_points,
    calc_occupation_points,
    get_occupation,
)

logger = logging.getLogger(__name__)

# 建卡时不可分配的技能：克苏鲁神话恒为 0；母语 = EDU，由引擎设置
NON_ALLOCATABLE = {"克苏鲁神话", "母语"}

BACKSTORY_LABELS = {
    "personalDescription": "个人描述",
    "ideologyBeliefs": "思想/信念",
    "significantPeople": "重要之人",
    "meaningfulLocations": "意义非凡之地",
    "treasuredPossessions": "宝贵之物",
    "traits": "特点",
}

# LLM 不可用 / 非法职业时的兜底职业
DEFAULT_OCCUPATION_BY_ERA = {
    "1920s": "记者",
    "现代": "记者",
    "modern": "记者",
}


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #
async def generate_ai_character(
    db: Session, module_id: str, hint: str = "", is_player: bool = False,
) -> dict[str, Any]:
    """生成一张完整、规则合法的角色卡草稿（不落库）。

    返回与 excel_import 一致的扁平结构：
    name / age / base_attributes / skills / system_data / backstory。
    is_player 暂为后续阶段（AI 队友直接落库）预留，阶段 0 前端走正常创建流程。
    """
    module = db.get(Module, module_id)
    if not module:
        raise ValueError("模组不存在")

    attrs = roll_attributes()

    ai: dict | None = None
    try:
        llm = get_llm()
        raw = await llm.complete(
            messages=[{"role": "user", "content": _build_prompt(module, hint, attrs)}],
            response_format={"type": "json_object"},
            temperature=0.8,
            max_tokens=2048,
        )
        ai = _parse_json(raw)
    except Exception:
        logger.exception("AI 建卡 LLM 调用失败: module=%s", module_id)

    if ai is None:
        return _rule_only_fallback(module, attrs)

    return _assemble(module, attrs, ai)


# --------------------------------------------------------------------------- #
# Prompt 构造
# --------------------------------------------------------------------------- #
def _build_prompt(module: Module, hint: str, attrs: dict[str, int]) -> str:
    era = (module.world_setting or {}).get("era", "1920s")
    occ_names = "、".join(o.name for o in COC_OCCUPATIONS)
    skill_names = "、".join(k for k in COC_DEFAULT_SKILLS if k not in NON_ALLOCATABLE)
    attrs_str = "、".join(f"{k}={v}" for k, v in attrs.items())
    hint_line = f"\n## 角色概念提示\n{hint}\n" if hint and hint.strip() else ""

    return f"""你是一位资深的克苏鲁的呼唤（CoC）七版守秘人，需要为玩家快速生成一张贴合模组背景的调查员角色卡。

## 模组信息
- 标题：{module.title}
- 年代：{era}
- 简介：{module.description or "（无）"}

## 已为该角色掷好的属性（请勿改动，仅作为设计参考）
{attrs_str}
{hint_line}
## 你的任务
基于以上属性与模组背景，设计一个**贴合 {era} 年代**、有血有肉的调查员：
1. occupation：从下列职业中选一个最合适的，必须原样返回其中之一：
{occ_names}
2. skills：列出 8-15 个你想加点的技能及其**目标总值**（整数 1-90）。技能名必须来自下列名单，禁止虚构、禁止带括号子项以外的写法：
{skill_names}
3. credit_rating：信用评级整数（会被钳制到所选职业的合理范围）
4. name / age（15-89，且与设定相符）/ gender
5. 六段结构化背景，各 1-2 句中文：personalDescription、ideologyBeliefs、significantPeople、meaningfulLocations、treasuredPossessions、traits
6. equipment：5-10 件贴合职业与 {era} 年代的随身物品名（字符串数组，物品须符合年代，不要武器以外的现代物品）

注意：{era} 年代不应出现不符合时代的科技、概念或物品。

## 返回格式（严格 JSON，不要任何额外文字或解释）
{{"name":"","age":25,"gender":"","occupation":"","credit_rating":0,"skills":{{"侦查":60,"聆听":50}},"equipment":["笔记本","怀表"],"personalDescription":"","ideologyBeliefs":"","significantPeople":"","meaningfulLocations":"","treasuredPossessions":"","traits":"","backstory":""}}"""


# --------------------------------------------------------------------------- #
# JSON 解析（三级容错）
# --------------------------------------------------------------------------- #
def _parse_json(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# --------------------------------------------------------------------------- #
# 组装 + 技能钳制（合法性保证全在这里）
# --------------------------------------------------------------------------- #
def _assemble(module: Module, attrs: dict[str, int], ai: dict) -> dict[str, Any]:
    age = _clamp_int(ai.get("age"), 15, 89, 25)
    dex = attrs.get("DEX", 50)
    edu = attrs.get("EDU", 50)

    occ = get_occupation(str(ai.get("occupation", "")))
    if occ is None:
        occ = _default_occupation(module)
    occ_name = occ.name

    cr = _clamp_int(ai.get("credit_rating"), occ.credit_min, occ.credit_max, occ.credit_min)
    occ_points = calc_occupation_points(occ.name, attrs)
    int_points = calc_interest_points(attrs)
    occ_points_remaining = max(0, occ_points - cr)  # 信用评级占用本职点

    base = build_default_skills(attrs)
    base["闪避"] = dex // 2          # 规避 build_default_skills 的 DEX 取值 bug
    base["信用评级"] = 0

    # 仅保留名单内、可分配的技能
    ai_skills_raw = ai.get("skills")
    ai_skills: dict[str, Any] = ai_skills_raw if isinstance(ai_skills_raw, dict) else {}
    ai_skills = {
        k: v for k, v in ai_skills.items()
        if k in base and k not in NON_ALLOCATABLE and k != "信用评级"
    }

    fixed_occ = set(occ.skills)
    extra = [k for k in ai_skills if k not in fixed_occ]
    chosen_extra = set(extra[: occ.choices]) if occ.choices > 0 else set()
    effective_occ = fixed_occ | chosen_extra

    occ_delta: dict[str, int] = {}
    int_delta: dict[str, int] = {}
    for name, target in ai_skills.items():
        try:
            t = int(target)
        except (ValueError, TypeError):
            continue
        delta = max(0, t - base.get(name, 0))
        if delta == 0:
            continue
        if name in effective_occ:
            occ_delta[name] = delta
        else:
            int_delta[name] = delta

    occ_delta = _scale_to_budget(occ_delta, occ_points_remaining)
    int_delta = _scale_to_budget(int_delta, int_points)

    final = dict(base)
    for k, d in {**occ_delta, **int_delta}.items():
        final[k] = base.get(k, 0) + d
    final["信用评级"] = cr
    final["母语"] = edu
    for k in list(final):
        if k != "克苏鲁神话":
            final[k] = min(90, max(0, final[k]))
    final["克苏鲁神话"] = 0

    return _build_result(attrs, age, occ_name, cr, final, ai)


def _build_result(
    attrs: dict[str, int], age: int, occ_name: str, cr: int,
    skills: dict[str, int], ai: dict,
) -> dict[str, Any]:
    system_data = compute_derived(attrs, age)
    system_data["occupation"] = occ_name
    system_data["creditRating"] = cr
    gender = str(ai.get("gender") or "").strip()
    if gender:
        system_data["gender"] = gender

    backstory_parts: list[str] = []
    for key, label in BACKSTORY_LABELS.items():
        val = str(ai.get(key) or "").strip()
        if val:
            system_data[key] = val
            backstory_parts.append(f"【{label}】{val}")
    free = str(ai.get("backstory") or "").strip()
    if free:
        backstory_parts.append(free)

    eq_raw = ai.get("equipment")
    equipment = (
        [str(e).strip() for e in eq_raw if str(e).strip()][:12]
        if isinstance(eq_raw, list) else []
    )
    if equipment:
        system_data["equipment"] = equipment

    return {
        "name": str(ai.get("name") or "无名调查员").strip(),
        "age": age,
        "base_attributes": attrs,
        "skills": skills,
        "system_data": system_data,
        "backstory": "\n".join(backstory_parts),
        "equipment": equipment,
    }


# --------------------------------------------------------------------------- #
# 兜底：LLM 完全不可用时，用纯规则产出一张合法卡
# --------------------------------------------------------------------------- #
def _rule_only_fallback(module: Module, attrs: dict[str, int]) -> dict[str, Any]:
    occ = _default_occupation(module)
    dex = attrs.get("DEX", 50)
    edu = attrs.get("EDU", 50)
    cr = occ.credit_min
    occ_points_remaining = max(0, calc_occupation_points(occ.name, attrs) - cr)

    base = build_default_skills(attrs)
    base["闪避"] = dex // 2
    base["信用评级"] = cr
    base["母语"] = edu

    occ_skills = [s for s in occ.skills if s in base and s not in NON_ALLOCATABLE]
    if occ_skills and occ_points_remaining > 0:
        per = occ_points_remaining // len(occ_skills)
        for s in occ_skills:
            base[s] = base.get(s, 0) + per

    for k in list(base):
        if k != "克苏鲁神话":
            base[k] = min(90, max(0, base[k]))
    base["克苏鲁神话"] = 0

    system_data = compute_derived(attrs, 25)
    system_data["occupation"] = occ.name
    system_data["creditRating"] = cr

    return {
        "name": "待命名调查员",
        "age": 25,
        "base_attributes": attrs,
        "skills": base,
        "system_data": system_data,
        "backstory": "",
        "_fallback": True,
    }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _scale_to_budget(deltas: dict[str, int], budget: int) -> dict[str, int]:
    """把一组技能增量按比例缩放到预算内，向下取整保证不超。"""
    total = sum(deltas.values())
    if total <= budget or total == 0:
        return deltas
    f = budget / total
    return {k: int(v * f) for k, v in deltas.items()}


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(val)
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, v))


def _default_occupation(module: Module) -> Occupation:
    era = (module.world_setting or {}).get("era", "1920s")
    occ = get_occupation(DEFAULT_OCCUPATION_BY_ERA.get(era, "记者"))
    return occ or COC_OCCUPATIONS[0]
