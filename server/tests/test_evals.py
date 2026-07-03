"""评估回路的免费部分单测：确定性检查、fixture 重建、上下文构建（不调 LLM）。"""

from pathlib import Path

from evals import checks
from evals.common import dict_to_model, load_fixture, row_to_dict
from evals.judge import _parse_judge_output, build_judge_messages
from evals.run import build_replay_messages

from app.models import EventLog

FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_study_search.json"


# ── 确定性检查 ──


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


class TestInternalIds:
    def test_泄漏flag标识判错(self):
        assert _errors(checks.check_internal_ids("你注意到 flag_secret_drawer 被触发了。"))

    def test_泄漏场景与npc裸id判错(self):
        assert _errors(checks.check_internal_ids("格雷夫斯（npc_butler_graves）看着你。"))
        assert _errors(checks.check_internal_ids("你来到 scene_study。"))

    def test_指令参数里的id合法(self):
        text = "他退后一步。[NPC_ACT: npc_id=npc_butler_graves, trigger=被质问]"
        assert not _errors(checks.check_internal_ids(text))

    def test_正常叙事不误报(self):
        assert not _errors(checks.check_internal_ids("灰雾漫过庄园的台阶，你敲了敲门。"))


class TestReportStyle:
    def test_汇报体判错(self):
        text = "【本轮进展】\n- 发现了暗格\n- 管家变得警惕\n"
        assert _errors(checks.check_report_style(text))

    def test_自然叙事不误报(self):
        assert not checks.check_report_style("书桌侧板发出空洞的回响，你的指尖停住了。")


class TestCommandSyntax:
    def test_未知指令判错(self):
        assert _errors(checks.check_command_syntax("[REVEAL_CLUE: id=xxx]"))

    def test_DICE_CHECK缺skill判错(self):
        assert _errors(checks.check_command_syntax("[DICE_CHECK: difficulty=normal]"))

    def test_合法指令通过(self):
        text = "你俯身细听。[DICE_CHECK: skill=侦查, difficulty=normal]"
        assert not checks.check_command_syntax(text)

    def test_SAY不配对给警告(self):
        findings = checks.check_command_syntax("[SAY: who=格雷夫斯]请随我来。")
        assert findings and all(f.severity == "warn" for f in findings)

    def test_中文叙事中的方括号旁白不误报(self):
        # 小写/非指令形状的方括号内容不应被当成指令
        assert not checks.check_command_syntax("他递来一张纸条[字迹潦草]。")


class TestEventEcho:
    def _errs(self, text):
        return [f for f in checks.check_event_echo(text) if f.severity == "error"]

    def test_回显玩家行动格式判错(self):
        assert self._errs("[亨利·卡特 行动] 我重点关注侧板接缝。")

    def test_回显玩家发言格式判错(self):
        assert self._errs("话音落下。[格雷夫斯 发言] 「请随我来」")

    def test_正常方括号旁白不误报(self):
        assert not self._errs("他递来一张纸条[字迹潦草]。")

    def test_指令标签不误报(self):
        assert not self._errs("你俯身细听。[DICE_CHECK: skill=聆听]")


class TestPlayerControl:
    NAMES = ["亨利·卡特"]

    def test_替玩家开口命中(self):
        text = "亨利·卡特说：「我们分头找。」"
        assert checks.check_player_control(text, self.NAMES)

    def test_npc对玩家说话不误报(self):
        text = "格雷夫斯对亨利·卡特说：「请随我来。」"
        assert not checks.check_player_control(text, self.NAMES)

    def test_环境描写不误报(self):
        text = "亨利·卡特的敲击声在书房里回荡。"
        assert not checks.check_player_control(text, self.NAMES)


# ── fixture 重建与上下文构建 ──


class TestFixtureRoundtrip:
    def test_合成fixture可加载重建(self):
        case = load_fixture(SYNTHETIC)
        assert case.name == "synthetic_study_search"
        assert case.player_char.name == "亨利·卡特"
        assert case.session.current_scene_id == "scene_study"
        assert len(case.events) == 6
        assert case.plan is not None and case.plan.requires_check
        assert "kp_core" in case.tags

    def test_重放消息可构建且含关键材料(self):
        case = load_fixture(SYNTHETIC)
        messages = build_replay_messages(case)
        assert messages, "上下文为空"
        joined = "\n".join(m.get("content") or "" for m in messages)
        assert "灰雾庄园" in joined            # 模组进了上下文
        assert "敲击书桌" in joined            # 最新玩家输入进了上下文
        assert case.plan.check.skill in joined  # 预存计划注入了

    def test_row_to_dict往返一致(self):
        case = load_fixture(SYNTHETIC)
        e = case.events[0]
        rebuilt = dict_to_model(EventLog, row_to_dict(e))
        assert rebuilt.sequence_num == e.sequence_num
        assert rebuilt.content == e.content
        assert rebuilt.metadata_ == e.metadata_

    def test_多余键向后兼容(self):
        data = row_to_dict(load_fixture(SYNTHETIC).events[0])
        data["future_field"] = "新版本才有的字段"
        rebuilt = dict_to_model(EventLog, data)
        assert rebuilt.event_type == "narration"


# ── 裁判输出解析（不调 LLM）──


class TestJudgeParsing:
    def test_构建裁判消息含旁白与计划(self):
        case = load_fixture(SYNTHETIC)
        msgs = build_judge_messages(case, case.plan, "一段旁白")
        joined = "\n".join(m["content"] for m in msgs)
        assert "一段旁白" in joined
        assert "侦查" in joined

    def test_解析完整输出(self):
        raw = (
            '{"no_leak": {"pass": true, "reason": ""},'
            '"plan_adherence": {"pass": false, "reason": "没发检定"},'
            '"no_player_control": {"pass": true, "reason": ""},'
            '"in_character": {"pass": true, "reason": ""},'
            '"coherence": {"pass": true, "reason": ""}}'
        )
        parsed = _parse_judge_output(raw)
        assert parsed and not parsed["plan_adherence"]["pass"]

    def test_解析带代码栅栏的输出(self):
        raw = '```json\n{"no_leak": {"pass": true}, "plan_adherence": {"pass": true}, "no_player_control": {"pass": true}, "in_character": {"pass": true}, "coherence": {"pass": true}}\n```'
        assert _parse_judge_output(raw)

    def test_缺项返回None(self):
        assert _parse_judge_output('{"no_leak": {"pass": true}}') is None

    def test_非JSON返回None(self):
        assert _parse_judge_output("这不是 JSON") is None
