"""KP 回合校验器（阶段 2 Turn Validator）的回归测试。

只验证结构化校验层与预筛逻辑，不依赖真实 LLM。
"""

import pytest

from app.ai import turn_validator
from app.ai.turn_planner import SafetyPolicy, TurnPlan


class _FakeLLM:
    def __init__(self, resp=None, raise_error=False):
        self.resp = resp
        self.raise_error = raise_error
        self.called = False

    async def complete(self, messages, temperature=0, response_format=None, max_tokens=None):
        self.called = True
        if self.raise_error:
            raise RuntimeError("provider down")
        return self.resp


class _Ev:
    def __init__(self, etype, content):
        self.event_type, self.content = etype, content


def test_recent_seen_text_takes_recent_visible_events():
    """_recent_seen_text 只取近期玩家可见事件（旁白/台词/骰点）的正文，system/action 不计。"""
    from app.services.chat_service import _recent_seen_text

    events = [
        _Ev("narration", "灰雾漫过站台。"),
        _Ev("system", "flag 内部标记"),          # 不该进（非玩家叙事可见口径）
        _Ev("dialogue", "「快跑！」"),
        _Ev("narration", "尽头出现一张巨口。"),
    ]
    out = _recent_seen_text(events, limit=6)
    assert "巨口" in out and "快跑" in out and "灰雾" in out
    assert "flag 内部标记" not in out
    assert _recent_seen_text([], limit=6) == ""


def test_prompt_distinguishes_witnessed_from_truth():
    """校验器指令要分清「亲历的感官现象」（不拦）与「点破隐藏真相」（拦）。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["那是奈亚的化身"]))
    msgs = turn_validator.build_validator_messages(
        plan, "龙牙看着那张巨口吞下整列电车。", seen_context="站台尽头出现了一张巨口",
    )
    joined = "\n".join(m["content"] for m in msgs)
    assert "亲历" in joined and "点破" in joined       # 分清两者
    assert "不违规" in joined                          # 明确「不拦亲历现象」的一侧
    assert "站台尽头出现了一张巨口" in joined           # seen_context 已嵌入
    assert "那是奈亚的化身" in joined                   # do_not_reveal 仍传达


@pytest.mark.asyncio
async def test_seen_context_threaded_to_validator_prompt():
    """seen_context 应贯通到校验器 prompt，让它别把玩家已看到的内容当泄露。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["隐藏真相"]))
    captured: dict = {}

    class _Cap:
        called = True

        async def complete(self, messages, **kw):
            captured["m"] = messages
            return '{"violated": false}'

    await turn_validator.validate_turn_narration(
        _Cap(), plan, "一段旁白", seen_context="玩家上一轮已目睹的怪影",
    )
    joined = "\n".join(m["content"] for m in captured["m"])
    assert "玩家上一轮已目睹的怪影" in joined


@pytest.mark.asyncio
async def test_validate_returns_none_without_plan():
    llm = _FakeLLM()
    result = await turn_validator.validate_turn_narration(llm, None, "任意旁白")
    assert result is None
    assert llm.called is False


@pytest.mark.asyncio
async def test_validate_skips_llm_call_when_not_suspicious():
    """没有 do_not_reveal、也没有汇报体/内部标识痕迹时，不必为每轮都多花一次调用。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=[]))
    llm = _FakeLLM()
    result = await turn_validator.validate_turn_narration(
        llm, plan, "你推开门，昏暗的门厅里弥漫着灰尘的气味。",
    )
    assert result is None
    assert llm.called is False


@pytest.mark.asyncio
async def test_validate_calls_llm_when_do_not_reveal_present():
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["管家就是纵火者"]))
    llm = _FakeLLM(resp='{"violated": false, "reason": "", "corrected_narration": ""}')
    result = await turn_validator.validate_turn_narration(llm, plan, "管家看起来有些紧张。")
    assert llm.called is True
    assert result is not None
    assert result.violated is False


@pytest.mark.asyncio
async def test_validate_detects_report_style_leak_without_do_not_reveal():
    """曾出现的真实 bug：KP 把裁定计划当汇报，输出【场景状态更新】+ 要点列表，泄露 flag 名。
    即使 do_not_reveal 为空，这种「汇报体」格式本身也该被启发式预筛识别为可疑，值得校验。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=[]))
    leaked = (
        "【场景状态更新】\n"
        "- 无关键线索被直接揭示。flag hint_leviticus_25_10 仍需调查员以其他方式获取。"
    )
    llm = _FakeLLM(resp=(
        '{"violated": true, "reason": "汇报体+内部flag id泄露",'
        ' "corrected_narration": "房间陷入沉默，没有人再说话。"}'
    ))
    result = await turn_validator.validate_turn_narration(llm, plan, leaked)
    assert llm.called is True
    assert result is not None
    assert result.violated is True
    assert "flag" not in result.corrected_narration


@pytest.mark.asyncio
async def test_validate_fails_open_on_bad_json():
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(resp="不是 JSON")
    result = await turn_validator.validate_turn_narration(llm, plan, "旁白内容")
    assert result is None  # 解析失败按放行处理，不阻塞跑团


@pytest.mark.asyncio
async def test_validate_fails_open_on_call_error():
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(raise_error=True)
    result = await turn_validator.validate_turn_narration(llm, plan, "旁白内容")
    assert result is None


@pytest.mark.asyncio
async def test_validate_falls_back_to_original_when_corrected_missing():
    """模型判定违规却没给改写文本时，别把旁白整段清空——退回原文总比清空强。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(resp='{"violated": true, "reason": "泄露秘密", "corrected_narration": ""}')
    result = await turn_validator.validate_turn_narration(llm, plan, "原始旁白内容")
    assert result.violated is True
    assert result.corrected_narration == "原始旁白内容"


@pytest.mark.asyncio
async def test_validate_parses_markdown_fenced_json():
    """模型常无视 json_object、把结果裹进 ```json 围栏——稳健抠取应能解析。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(resp='```json\n{"violated": false}\n```')
    result = await turn_validator.validate_turn_narration(llm, plan, "管家看起来有些紧张。")
    assert result is not None
    assert result.violated is False


@pytest.mark.asyncio
async def test_validate_accepts_minimal_not_violated_shape():
    """新契约：不违规时只回 {"violated": false}，不回填旁白也应校验通过。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(resp='{"violated": false}')
    result = await turn_validator.validate_turn_narration(llm, plan, "旁白内容")
    assert result is not None
    assert result.violated is False
    assert result.corrected_narration == ""


@pytest.mark.asyncio
async def test_validate_fails_open_on_truncated_json():
    """复现线上 bug：输出被截成半截字符串（Unterminated string）→ 抠不出完整 JSON → 放行。"""
    plan = TurnPlan(safety=SafetyPolicy(do_not_reveal=["秘密"]))
    llm = _FakeLLM(resp='{"violated": true, "reason": "泄露了秘密的具体内容但这段话还没写完')
    result = await turn_validator.validate_turn_narration(llm, plan, "旁白内容")
    assert result is None  # 截断无法解析，按放行处理，不阻塞跑团
