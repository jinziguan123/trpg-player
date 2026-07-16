"""评估回路的免费部分单测：确定性检查、fixture 重建、上下文构建（不调 LLM）。"""

from pathlib import Path

import pytest

from evals import checks
from evals.common import dict_to_model, load_fixture, row_to_dict
from evals.judge import RUBRIC, _parse_judge_output, build_judge_messages
from evals.run import build_replay_messages

from app.ai.context import build_kp_context
from app.ai.prompts.kp_system import KP_DICE_CONTINUATION_PROMPT
from app.models import EventLog

FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_study_search.json"
MULTI_ACTOR_CONT = FIXTURES / "manor_multi_actor_int_continuation.json"
SPLIT_PERCEPTION = FIXTURES / "manor_split_npc_perception.json"
IMPROV_PROBE = FIXTURES / "manor_improvised_npc_probe.json"


# ── 确定性检查 ──


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


class TestPlanAdjudication:
    """planner 裁定断言：any_of 任一子句满足即通过，否则 error。"""

    def test_惩罚骰满足期望通过(self):
        plan = {"check": {"penalty": 1, "difficulty": "normal"}, "auto_outcome": "none",
                "requires_check": True}
        expect = {"any_of": [{"path": "check.penalty", "op": ">=", "value": 1},
                             {"path": "auto_outcome", "op": "==", "value": "failure"}]}
        assert not _errors(checks.check_plan_adjudication(plan, expect))

    def test_升难度用in子句满足(self):
        plan = {"check": {"penalty": 0, "difficulty": "extreme"}}
        expect = {"any_of": [{"path": "check.difficulty", "op": "in",
                              "value": ["hard", "extreme"]}]}
        assert not _errors(checks.check_plan_adjudication(plan, expect))

    def test_都不满足判错(self):
        plan = {"check": {"penalty": 0, "difficulty": "normal"}, "auto_outcome": "none"}
        expect = {"note": "应变难", "any_of": [
            {"path": "check.penalty", "op": ">=", "value": 1},
            {"path": "auto_outcome", "op": "==", "value": "failure"}]}
        assert _errors(checks.check_plan_adjudication(plan, expect))

    def test_无期望或无计划(self):
        assert checks.check_plan_adjudication({"x": 1}, None) == []          # 无期望→跳过
        assert _errors(checks.check_plan_adjudication(None, {"any_of": []}))  # 无计划→判错


class TestSampleAggregation:
    """多次采样聚合：通过率 + 稳过判定 + 失败原因计数（纯函数，不调 LLM）。"""

    def test_聚合通过率与失败原因(self):
        from evals.run import aggregate_samples
        samples = [
            {"passed": True, "findings": [], "judge": None},
            {"passed": False, "judge": None,
             "findings": [{"check": "plan_adjudication", "severity": "error", "detail": "x"}]},
            {"passed": False, "judge": None,
             "findings": [{"check": "plan_adjudication", "severity": "error", "detail": "y"}]},
        ]
        agg = aggregate_samples("f", ["t"], samples)
        assert agg["runs"] == 3 and agg["pass_count"] == 1
        assert abs(agg["pass_rate"] - 1 / 3) < 1e-9
        assert agg["passed"] is False                 # 非全过 → 不算稳过
        assert "plan_adjudication×2" in agg["detail"]

    def test_全过才算稳过(self):
        from evals.run import aggregate_samples
        agg = aggregate_samples(
            "f", [], [{"passed": True, "findings": []}, {"passed": True, "findings": []}])
        assert agg["passed"] is True and agg["pass_rate"] == 1.0 and agg["detail"] == ""


class TestAdjudicationFixtures:
    def test_两条裁定fixture带plan_expect且无预存plan(self):
        """裁定评测 fixture 必须现跑 planner（无预存 plan）并携带 plan_expect，否则评不到裁定。"""
        for name in ("synthetic_stealth_after_noise", "synthetic_persuade_strong_rp"):
            case = load_fixture(FIXTURES / f"{name}.json")
            assert case.plan is None and case.plan_expect and case.plan_expect.get("any_of")


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


class TestAntithesisTic:
    """否定式对比句式（不是X是Y）过度复用探针：只测密集复用，单次与普通否定不报。"""

    def test_密集否定对比给警告(self):
        text = ("墙上的划痕不是随意的，而是某种刻意的排列。"
                "那不是装饰，是警告。")
        findings = checks.check_antithesis_tic(text)
        assert findings and all(f.severity == "warn" for f in findings)
        assert "2" in findings[0].detail  # detail 带出复用次数，供 scorecard 量化

    def test_单次点睛不报(self):
        assert not checks.check_antithesis_tic("那不是脚步声，而是某种更沉的东西从楼板下传来。")

    def test_普通否定与跨主语并列不误报(self):
        # 「这不是钥匙」是普通否定；「不是本地人，房子是租的」是跨主语并列，均非对比 tic。
        assert not checks.check_antithesis_tic("这不是钥匙。他不是本地人，房子是租的。")

    def test_与其说不如说及这不是这是计入(self):
        text = "与其说这是巧合，不如说是宿命。这不是结束，这是开始。"
        assert checks.check_antithesis_tic(text)

    def test_指令内文本不参与统计(self):
        # tic 统计前先剥离方括号指令，避免指令参数里的字符干扰。
        text = "他递上纸条。[DICE_CHECK: skill=侦查]"
        assert not checks.check_antithesis_tic(text)


class TestNarrationStyleVariety:
    def test_kp提示词含文风忌单一约束(self):
        from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT
        p = KP_SYSTEM_PROMPT
        assert "文风忌单一（硬规则）" in p          # 已提为顶层硬规则
        assert "否定式对比" in p and "而是" in p    # 点名了 tic
        assert "整轮叙述至多用一次" in p            # 硬性上限（此前是「至多一次」的软约束）


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


# ── 投骰后续写：主语归属（复现线上 subject-drift bug）──


class TestContinuationSubjectFidelity:
    def test_续写提示词钉死主语为检定执行者(self):
        # 修复的核心：续写提示词必须把「叙述主语=检定执行者」写成硬约束（防措辞被静默删除）。
        p = KP_DICE_CONTINUATION_PROMPT
        assert "叙述主语" in p and "执行者" in p
        assert "安到别的角色" in p  # 明确禁止张冠李戴

    def test_judge_含主语归属评分项(self):
        assert "subject_fidelity" in RUBRIC

    def test_续写fixture可加载且带continuation(self):
        case = load_fixture(MULTI_ACTOR_CONT)
        assert case.player_char.name == "伊芙琳·哈特"
        assert [t.name for t in case.teammates] == ["亨利·卡特"]
        assert case.continuation and "伊芙琳·哈特" in case.continuation
        assert "智力" in case.continuation

    def test_续写上下文可构建且含双方检定线索(self):
        # 上下文里应同时能看到「亨利掷了侦查」「伊芙琳掷了智力」，judge 才能判断主语该归谁。
        case = load_fixture(MULTI_ACTOR_CONT)
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        joined = "\n".join(m.get("content") or "" for m in messages)
        assert "伊芙琳·哈特" in joined and "亨利·卡特" in joined
        assert "智力" in joined and "侦查" in joined


# ── NPC 感知边界（复现线上「隔墙有耳」bug）──


class TestNpcPerceptionIsolation:
    def test_kp提示词含感知边界硬规则(self):
        from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT
        p = KP_SYSTEM_PROMPT
        assert "感知边界" in p
        assert "当面告诉" in p and "隔墙可闻" in p
        assert "感知之外的事" in p  # 明确禁止 NPC 反应视野外事件

    def test_分头提示词含NPC隔离条款(self):
        from app.services.chat_service import SPLIT_FOCUS_PROMPT
        assert "一无所知" in SPLIT_FOCUS_PROMPT

    def test_judge_含感知边界评分项(self):
        assert "perception_isolation" in RUBRIC

    def test_分处两地时注入人员分布(self):
        case = load_fixture(SPLIT_PERCEPTION)
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        sys_msg = messages[0]["content"]
        assert "当前人员分布" in sys_msg
        assert "书房" in sys_msg and "走廊" in sys_msg
        assert "格雷夫斯" in sys_msg          # 走廊侧列出了在场 NPC
        assert "看不见、听不见" in sys_msg     # 感知隔断提醒

    def test_全员同处不注入分布(self):
        # 用同处一地的多人 fixture（智力续写那份：两人都在前室）验证不注入——行为不变。
        case = load_fixture(MULTI_ACTOR_CONT)
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        assert "当前人员分布" not in messages[0]["content"]


# ── 临场 NPC 收容（复现线上「编造 NPC 存在感雪球」）──


class TestImprovisedNpcContainment:
    def test_kp提示词含临场角色纪律(self):
        from app.ai.prompts.kp_system import KP_SYSTEM_PROMPT
        p = KP_SYSTEM_PROMPT
        assert "临场角色纪律" in p
        assert "带货不行" in p          # 指路可以、带货不行
        assert "不升级" in p            # 反复互动不升级重要性

    def test_judge_含收容评分项(self):
        assert "improvised_containment" in RUBRIC

    def test_record_improvised_npc_累加(self):
        from app.services import world_memory
        ws = world_memory.record_improvised_npc({}, "门房老赵", 3)
        assert ws["improvised_npcs"]["门房老赵"]["first_seq"] == 3
        assert ws["improvised_npcs"]["门房老赵"]["mentions"] == 1
        ws = world_memory.record_improvised_npc(ws, "门房老赵", 5)
        e = ws["improvised_npcs"]["门房老赵"]
        assert e["first_seq"] == 3 and e["last_seq"] == 5 and e["mentions"] == 2
        assert world_memory.record_improvised_npc(ws, "  ", 6) is ws or True  # 空名不崩

    def test_implausible_names_不登记(self):
        """台词归属误命中的垃圾名（代词/动词短语/结构指称/旁白碎片）不得登记为临场 NPC。"""
        from app.services import world_memory
        for bad in ["她", "修女在回", "但字距稍疏", "第七节", "他们", "一"]:
            ws = world_memory.record_improvised_npc({}, bad, 1)
            assert not (ws.get("improvised_npcs") or {}), f"垃圾名不该登记：{bad}"
        for good in ["护士长", "前台女士", "管理员", "门房老赵", "玛格丽特修女"]:
            ws = world_memory.record_improvised_npc({}, good, 1)
            assert good in (ws.get("improvised_npcs") or {}), f"真龙套应登记：{good}"

    def test_list_improvised_滤除存量垃圾名(self):
        """读取侧兼容存量：已登记的垃圾名不出现在可收编列表（写入侧现已挡掉）。"""
        from app.services import promote_service
        from app.models import Character, GameSession, Module, Base
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng)()
        module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
        pc = Character(name="伊芙琳", rule_system="coc", is_player=True)
        db.add_all([module, pc]); db.flush()
        gs = GameSession(
            module_id=module.id, player_character_id=pc.id, status="active",
            world_state={"improvised_npcs": {
                "护士长": {"mentions": 2},
                "她": {"mentions": 1},          # 存量垃圾
                "第七节": {"mentions": 1},        # 存量垃圾
            }},
        )
        db.add(gs); db.commit()
        names = [x["name"] for x in promote_service.list_improvised(db, gs.id)]
        assert names == ["护士长"]  # 垃圾名被滤掉

    def test_record_npc_say_memory_登记非正典说话人(self):
        # 说话人不在 module.npcs、不是玩家/系统 → 登记进 improvised_npcs；正典/玩家/系统不登记。
        from app.services import chat_service
        from app.models import Character, GameSession, Module
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import Base
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng)()
        module = Module(title="M", rule_system="coc",
                        npcs=[{"id": "npc_g", "name": "格雷夫斯"}], scenes=[])
        pc = Character(name="伊芙琳·哈特", rule_system="coc", is_player=True)
        db.add_all([module, pc]); db.flush()
        gs = GameSession(module_id=module.id, player_character_id=pc.id,
                         status="active", world_state={})
        db.add(gs); db.commit()
        chat_service._record_npc_say_memory(
            db, gs.id, gs, module,
            [("门房老赵", "屋里的事我不晓得"),      # 临场龙套 → 登记
             ("格雷夫斯", "请随我来"),               # 正典 → 不登记进 improvised
             ("伊芙琳·哈特", "你说谎"),              # 玩家 → 不登记
             ("系统", "检定失败")],                  # 系统 → 不登记
            audience_names=["伊芙琳·哈特"],
        )
        improv = (db.get(GameSession, gs.id).world_state or {}).get("improvised_npcs") or {}
        assert "门房老赵" in improv
        assert "格雷夫斯" not in improv and "伊芙琳·哈特" not in improv and "系统" not in improv

    def test_上下文注入临场角色名单(self):
        case = load_fixture(IMPROV_PROBE)
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        sys_msg = messages[0]["content"]
        assert "临场角色名单" in sys_msg
        assert "门房老赵" in sys_msg
        assert "带货" in sys_msg or "不携带线索" in sys_msg

    def test_无临场角色不注入名单(self):
        case = load_fixture(SYNTHETIC)  # 无 improvised_npcs
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        assert "临场角色名单" not in messages[0]["content"]

    def test_planner_payload_含正典与临场名单(self):
        from app.ai import turn_planner
        case = load_fixture(IMPROV_PROBE)
        messages = turn_planner.build_turn_plan_messages(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        content = messages[1]["content"]
        assert "canonical_npcs" in content and "格雷夫斯" in content
        assert "improvised_npcs" in content and "门房老赵" in content
        assert "canonical_npcs" in content  # 指令提到只能用正典名字


# ── P2 受控转正 ──


def _promote_probe_case():
    """基于 IMPROV_PROBE，给「门房老赵」挂一张转正卡后返回 case。"""
    from app.services import world_memory
    case = load_fixture(IMPROV_PROBE)
    ws = world_memory.promote_improvised_npc(
        dict(case.session.world_state or {}),
        "门房老赵",
        {"name": "门房老赵", "description": "佝偻的守夜门房", "personality": "话少、怕事",
         "background": "在庄园守了二十年门"},
    )
    case.session.world_state = ws
    return case, ws


class TestImprovisedPromotion:
    def test_promote_card_secrets_恒空_且带id(self):
        from app.services import world_memory
        _, ws = _promote_probe_case()
        card = ws["improvised_npcs"]["门房老赵"]["card"]
        assert card["id"].startswith("improv_")
        assert card["secrets"] == []          # 转正不自动获得秘密
        cards = world_memory.promoted_npc_cards(ws)
        assert len(cards) == 1 and cards[0]["name"] == "门房老赵" and cards[0]["improvised"]

    def test_转正后从临场名单移除_并入KP正典资料(self):
        case, _ = _promote_probe_case()
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )
        sys_msg = messages[0]["content"]
        # 已转正 → 不再出现在「临场角色名单」小节
        assert "临场角色名单" not in sys_msg
        # 但作为正典 NPC 资料出现（description 进了 npcs_info）
        assert "门房老赵" in sys_msg and "佝偻的守夜门房" in sys_msg

    def test_转正后进planner正典名单_且不再算龙套(self):
        from app.ai import turn_planner
        case, _ = _promote_probe_case()
        content = turn_planner.build_turn_plan_messages(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
        )[1]["content"]
        payload = _extract_probe_payload(content)
        assert "门房老赵" in payload["canonical_npcs"]
        assert "门房老赵" not in payload["improvised_npcs"]

    def test_NPC_ACT_能解析到转正卡(self):
        from app.ai.context import build_npc_context
        case, ws = _promote_probe_case()
        card_id = ws["improvised_npcs"]["门房老赵"]["card"]["id"]
        msgs = build_npc_context(card_id, case.session, case.module, case.events)
        assert "门房老赵" in msgs[0]["content"]  # 人格提示用了转正卡的名字，非「未知NPC」

    @pytest.mark.asyncio
    async def test_generate_npc_card_强制name且secrets空(self):
        from app.ai import npc_promote

        class _LLM:
            async def complete(self, messages, **kw):
                # 模型故意改名 + 试图塞秘密，应被规整掉
                return ('{"name":"别的名字","description":"守夜人",'
                        '"personality":"寡言","background":"看门二十年",'
                        '"secrets":["其实是凶手"]}')
        card = await npc_promote.generate_npc_card(
            _LLM(), name="门房老赵", material="……", module_title="鬼屋",
        )
        assert card["name"] == "门房老赵"     # name 强制用传入值，不被模型改名
        assert card["secrets"] == []          # secrets 恒空，模型塞的秘密被丢弃
        assert card["personality"] == "寡言"


def _extract_probe_payload(content: str) -> dict:
    import json as _json
    start = content.index("{")
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return _json.loads(content[start:i + 1])
    raise AssertionError("payload 未找到")


# ── 裁判输出解析（不调 LLM）──


class TestJudgeParsing:
    def test_构建裁判消息含旁白与计划(self):
        case = load_fixture(SYNTHETIC)
        msgs = build_judge_messages(case, case.plan, "一段旁白")
        joined = "\n".join(m["content"] for m in msgs)
        assert "一段旁白" in joined
        assert "侦查" in joined

    def test_解析完整输出(self):
        import json as _json

        from evals.judge import RUBRIC
        # 从 RUBRIC 动态构造全维度输出（对未来新增评分项稳健），把 plan_adherence 设失败
        out = {k: {"pass": True, "reason": ""} for k in RUBRIC}
        out["plan_adherence"] = {"pass": False, "reason": "没发检定"}
        parsed = _parse_judge_output(_json.dumps(out))
        assert parsed and not parsed["plan_adherence"]["pass"]

    def test_解析带代码栅栏的输出(self):
        import json as _json

        from evals.judge import RUBRIC
        body = _json.dumps({k: {"pass": True} for k in RUBRIC})
        assert _parse_judge_output(f"```json\n{body}\n```")

    def test_缺项返回None(self):
        assert _parse_judge_output('{"no_leak": {"pass": true}}') is None

    def test_非JSON返回None(self):
        assert _parse_judge_output("这不是 JSON") is None
