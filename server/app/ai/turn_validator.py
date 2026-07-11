from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ai.turn_planner import TurnPlan, _extract_json_object

logger = logging.getLogger(__name__)


class TurnValidation(BaseModel):
    violated: bool = False
    reason: str = ""
    corrected_narration: str = ""


# 汇报体特征：全角标题（≤12 字）紧跟一段项目符号列表——KP 应该写故事，不该写这种总结报告。
_REPORT_STYLE_RE = re.compile(r"【[^】\n]{1,12}】\s*\n(?:\s*[-•]\s*.+\n?){1,}")
# 内部标识特征：flag_xxx / flag xxx 这类技术性 token，正常叙事文本不会写出来。
_INTERNAL_ID_RE = re.compile(r"\bflag[_ ][a-z0-9_]+", re.IGNORECASE)

# 否定式对比句式（"不是X，是Y" / "不是X而是Y" / "与其说…不如说…" / "这不是…，这是…"）：
# 各家 LLM 头号「显得文学」的口头禅，密集复用则空洞、审美疲劳。这是唯一真源——
# evals 的文风探针与 KP 上下文的反 tic 反馈环 nudge 都从这里取，避免规则各写一份漂移。
# 逗号前谓语段刻意排除逗号，避免「不是本地人，房子是租的」这类跨主语并列句误命中。
_ANTITHESIS_RE = re.compile(
    r"不是[^。！？；，\n]{1,30}?(?:而是|，(?:而是|却是|倒是|反倒是|反而是|才是|是))"
    r"|并非[^。！？；，\n]{1,30}?而是"
    r"|与其说[^。！？；\n]{1,30}?不如说"
    r"|这不是[^。！？；\n]{1,30}?，[^。！？；\n]{0,4}?这(?:是|才是)"
)


def count_antithesis(text: str) -> int:
    """统计一段文本里否定式对比句式的出现次数（文风单一化的量化指标）。"""
    return len(_ANTITHESIS_RE.findall(text or ""))


def _looks_suspicious(narration: str, plan: TurnPlan) -> bool:
    """零成本预筛：值不值得为这段旁白多花一次 LLM 校验调用。

    有硬性隐藏信息（safety.do_not_reveal）时，语义泄露的代价高，值得付这次调用成本；
    没有硬性隐藏信息时，只在文本已经露出「汇报体」或内部标识的明显痕迹时才校验，
    避免每轮都无谓地多跑一次调用。
    """
    if plan.safety.do_not_reveal:
        return True
    if _REPORT_STYLE_RE.search(narration):
        return True
    if _INTERNAL_ID_RE.search(narration):
        return True
    return False


def build_validator_messages(plan: TurnPlan, narration: str) -> list[dict]:
    do_not_reveal = json.dumps(plan.safety.do_not_reveal, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "你是 TRPG 内容安全校验器，检查一段旁白是否违反裁定计划的安全约束。"
                "只输出一个 JSON object，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                "安全约束（玩家不可见的隐藏信息，绝不能出现在旁白里，即使是转述/总结/暗示）：\n"
                f"{do_not_reveal}\n\n"
                "此外无论约束是否为空，以下两类都算违规：\n"
                "1. 用【标题】加项目符号列表的「汇报体」总结本回合状态/进展/待触发条件，而非自然叙事；\n"
                "2. 旁白里出现了 flag 名、线索/NPC 的内部 id、JSON 字段名等技术性标识。\n\n"
                f"待检查的旁白：\n{narration}\n\n"
                '不违规时只返回 {"violated": false}，不要回填旁白；\n'
                '违规时返回 {"violated": true, "reason": "简述违规之处", '
                '"corrected_narration": "改写后的旁白——去掉违规部分，'
                '尽量保留其余内容与叙事风格、少改动"}\n'
                "只输出 JSON。"
            ),
        },
    ]


async def validate_turn_narration(
    llm: Any, plan: TurnPlan | None, narration: str,
) -> TurnValidation | None:
    """校验一段已生成的旁白是否违反本轮裁定计划的硬约束，违反则给出改写版本。

    只对『落库/持久化的文本』生效——无法收回已经流式广播出去的内容，但能防止违规
    内容永久留在会话记录里（重连、其他玩家、复盘都会看到落库版本）。
    校验失败（无 LLM / 解析出错 / 调用异常）一律放行原文，不阻塞跑团。
    """
    if plan is None or llm is None or not narration.strip():
        return None
    if not _looks_suspicious(narration, plan):
        return None

    messages = build_validator_messages(plan, narration)
    try:
        # 不设 max_tokens 硬上限：推理类模型的 reasoning 会占输出预算，硬上限会把 JSON 截成半截
        # 字符串（线上「Unterminated string」正是如此）。交服务端默认上限，complete 已内部流式。
        raw = await llm.complete(
            messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("KP 回合校验器调用失败，按放行处理")
        return None

    # 稳健抠 JSON（剥围栏 / 夹带文字 / 已是 dict），比裸 json.loads 抗造；抠不出按放行处理。
    data = _extract_json_object(raw)
    if data is None:
        logger.warning("KP 回合校验器输出无法解析，按放行处理：%s", str(raw)[:200])
        return None
    try:
        result = TurnValidation.model_validate(data)
    except ValidationError as exc:
        logger.warning("KP 回合校验器输出不符合 schema，按放行处理：%s", exc)
        return None

    if result.violated and not result.corrected_narration.strip():
        # 兜底：模型判定违规却没给改写文本时，别把旁白整段清空
        result.corrected_narration = narration
    return result
