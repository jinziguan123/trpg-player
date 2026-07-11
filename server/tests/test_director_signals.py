"""导演信号（节奏经营）纯函数单测，以及 direction 注入到 planner/team 上下文的接线测试。

信号是确定性启发式，全部不调 LLM。
"""

from types import SimpleNamespace


from app.ai import director_signals as ds
from app.ai import turn_planner
from app.ai.context import build_team_context
from app.ai.turn_planner import DirectionPolicy, TurnPlan
from app.models import Base, Character, GameSession, Module  # noqa: F401


def _ev(seq, etype, actor="", content="", scene=None):
    return SimpleNamespace(
        sequence_num=seq, event_type=etype, actor_name=actor,
        content=content, metadata_={"scene_id": scene} if scene else {},
    )


# ── 聚光灯：冷场检测 ──


class TestSpotlight:
    def test_单人不判定(self):
        events = [_ev(i, "action", "亨利") for i in range(5)]
        assert ds.compute_spotlight_starved(events, ["亨利"]) == []

    def test_某玩家零出场且他人达阈值(self):
        events = [_ev(i, "action", "亨利") for i in range(4)]
        starved = ds.compute_spotlight_starved(events, ["亨利", "艾玛"])
        assert starved == ["艾玛"]

    def test_旁白点名算出场(self):
        events = [_ev(i, "action", "亨利") for i in range(4)]
        events.append(_ev(9, "narration", "KP", "艾玛靠近了窗边"))
        assert ds.compute_spotlight_starved(events, ["亨利", "艾玛"]) == []

    def test_他人未达阈值不判定(self):
        events = [_ev(0, "action", "亨利"), _ev(1, "action", "亨利")]
        assert ds.compute_spotlight_starved(events, ["亨利", "艾玛"]) == []


# ── 卡关检测 ──


class TestStuck:
    def test_无进展多回合判卡关(self):
        events = [_ev(i, "action", "亨利") for i in range(1, 5)]
        stuck, turns = ds.compute_stuck(events, {}, ["亨利"])
        assert stuck and turns == 4

    def test_线索发现后重置(self):
        events = [_ev(i, "action", "亨利") for i in range(1, 6)]
        ws = {"clue_ledger": {"c1": {"status": "known", "seq": 4}}}
        stuck, turns = ds.compute_stuck(events, ws, ["亨利"])
        assert turns == 1 and not stuck  # 只有 seq=5 一个玩家回合在进展之后

    def test_场景切换算进展(self):
        events = [
            _ev(1, "narration", "KP", "在门厅", scene="hall"),
            _ev(2, "action", "亨利"),
            _ev(3, "narration", "KP", "进入书房", scene="study"),
            _ev(4, "action", "亨利"),
        ]
        stuck, turns = ds.compute_stuck(events, {}, ["亨利"])
        assert turns == 1 and not stuck


# ── 未解悬念 ──


class TestThreads:
    def test_未触发trigger与partial线索(self):
        module = SimpleNamespace(
            triggers=[
                {"set_flags": ["f_done"], "description": "已触发的"},
                {"set_flags": ["f_todo"], "description": "血迹通向地窖"},
            ],
            clues=[{"id": "c1", "name": "半张地图"}],
        )
        ws = {"flags": ["f_done"], "clue_ledger": {"c1": {"status": "partial", "seq": 2}}}
        threads = ds.compute_unresolved_threads(module, ws)
        assert any("血迹通向地窖" in t for t in threads)
        assert any("半张地图" in t for t in threads)
        assert not any("已触发的" in t for t in threads)


# ── 节奏单调 ──


class TestMonotonous:
    def test_清一色调查判单调(self):
        events = []
        for i in range(1, 6):
            events.append(_ev(i * 2, "action", "亨利"))
            events.append(_ev(i * 2 + 1, "dice", "系统", "侦查检定"))
        assert ds.compute_monotonous(events, ["亨利"])

    def test_有npc对话不判单调(self):
        events = [_ev(i, "action", "亨利") for i in range(1, 6)]
        events.append(_ev(9, "dialogue", "管家", "「请随我来」"))
        assert not ds.compute_monotonous(events, ["亨利"])


# ── DirectorSignals 渲染 ──


class TestSignalsRender:
    def test_无信号不可注入(self):
        sig = ds.DirectorSignals()
        assert not sig.has_actionable()

    def test_有冷场则可注入且提示含角色名(self):
        sig = ds.DirectorSignals(spotlight_starved=["艾玛"])
        assert sig.has_actionable()
        assert "艾玛" in sig.to_prompt()


# ── 接线：direction 进 planner 渲染 / team_guidance 进队友上下文 ──


class TestDirectionCoercion:
    """direction 是软字段，模型常写错格式——归一保证不拖垮整份 TurnPlan。"""

    def test_pacing整句归一为枚举(self):
        assert DirectionPolicy(pacing="温和推进，给予信心").pacing == "tighten"
        assert DirectionPolicy(pacing="放缓节奏换口气").pacing == "release"
        assert DirectionPolicy(pacing="随便写的没关键词").pacing == "hold"

    def test_pacing合法值保持(self):
        assert DirectionPolicy(pacing="release").pacing == "release"

    def test_spotlight字符串归一为列表(self):
        assert DirectionPolicy(spotlight="伊芙琳·哈特（提问者）获得焦点。").spotlight == [
            "伊芙琳·哈特（提问者）获得焦点。"
        ]
        assert DirectionPolicy(spotlight="伊芙琳").spotlight == ["伊芙琳"]

    def test_spotlight列表去空保持(self):
        assert DirectionPolicy(spotlight=["伊芙琳", " ", "亨利"]).spotlight == ["伊芙琳", "亨利"]

    def test_nudge列表归一为字符串(self):
        assert DirectionPolicy(nudge=["让烛火摇曳", "NPC 走近"]).nudge == "让烛火摇曳；NPC 走近"

    def test_坏direction不拖垮整份plan(self):
        # 复现线上报错：pacing 是整句、spotlight 是字符串 → 归一后 TurnPlan 仍应校验通过
        plan = TurnPlan.model_validate({
            "requires_check": True,
            "check": {"skill": "侦查"},
            "safety": {"do_not_reveal": ["管家的秘密"]},
            "direction": {
                "pacing": "温和推进，给予信心，让玩家下一步决策。",
                "spotlight": "伊芙琳·哈特（提问者）获得焦点。",
            },
        })
        assert plan.requires_check and plan.check.skill == "侦查"
        assert plan.safety.do_not_reveal == ["管家的秘密"]  # 核心内容没被连累丢弃
        assert plan.direction.pacing == "tighten"
        assert plan.direction.spotlight == ["伊芙琳·哈特（提问者）获得焦点。"]


class TestTurnPlanShapeTolerance:
    """LLM 把嵌套字段写成一句话/标量时，只该退默认，不该整份计划被丢弃。"""

    def test_safety写成句子不拖垮计划(self):
        # 复现线上报错：safety='安全，无即时威胁。'
        plan = TurnPlan.model_validate({
            "requires_check": True,
            "check": {"skill": "聆听"},
            "safety": "安全，无即时威胁。",
        })
        assert plan.requires_check and plan.check.skill == "聆听"
        assert plan.safety.do_not_reveal == []  # 退默认
        assert plan.safety.do_not_control_players is True

    def test_safety写成safe(self):
        assert TurnPlan.model_validate({"safety": "safe"}).safety.do_not_reveal == []

    def test_各嵌套字段写成标量都归默认(self):
        plan = TurnPlan.model_validate({
            "check": "不需要检定",
            "clue_policy": "无线索",
            "npc_policy": "无人回应",
            "scene_policy": "不换场景",
        })
        assert plan.check.skill == "" and plan.clue_policy.reveal_level == "none"
        assert plan.npc_policy.speakers == [] and plan.scene_policy.scene_change is None

    def test_narration_brief写成字符串归一为列表(self):
        assert TurnPlan.model_validate(
            {"narration_brief": "描写敲击声"}
        ).narration_brief == ["描写敲击声"]

    def test_子模型列表字段写成null不拖垮计划(self):
        # 复现线上报错：npc_policy.speakers 是 null（default_factory 只在键缺失时生效，
        # 显式 null 会撞 list schema）。速度检定等核心内容不该被这个次要字段连累丢弃。
        plan = TurnPlan.model_validate({
            "requires_check": True,
            "check": {"skill": "侦查"},
            "npc_policy": {"speakers": None, "reaction": "管家警觉", "needs_npc_act": False},
            "clue_policy": {"candidate_clue_ids": None, "reveal_level": "hint"},
            "scene_policy": {"set_flags": None, "clear_flags": None},
            "safety": {"do_not_reveal": None},
            "narration_brief": None,
        })
        assert plan.requires_check and plan.check.skill == "侦查"
        assert plan.npc_policy.speakers == [] and plan.npc_policy.reaction == "管家警觉"
        assert plan.clue_policy.candidate_clue_ids == [] and plan.clue_policy.reveal_level == "hint"
        assert plan.scene_policy.set_flags == [] and plan.scene_policy.clear_flags == []
        assert plan.safety.do_not_reveal == [] and plan.narration_brief == []

    def test_turn_kind非法值退mixed(self):
        assert TurnPlan.model_validate({"turn_kind": "探案"}).turn_kind == "mixed"

    def test_合法计划完全不受影响(self):
        plan = TurnPlan.model_validate({
            "turn_kind": "investigate",
            "safety": {"do_not_reveal": ["暗格"]},
            "narration_brief": ["描写过程"],
        })
        assert plan.turn_kind == "investigate"
        assert plan.safety.do_not_reveal == ["暗格"]


class TestWiring:
    def test_plan_message含导演笔记(self):
        plan = TurnPlan(direction=DirectionPolicy(pacing="tighten", spotlight=["艾玛"]))
        msg = turn_planner.build_turn_plan_message(plan)
        assert "direction" in msg["content"]
        assert "导演笔记" in msg["content"]
        assert "艾玛" in msg["content"]

    def test_team_context注入导演提示(self):
        module = Module(
            title="t", rule_system="coc",
            scenes=[{"id": "hall", "name": "门厅", "description": "x"}],
            npcs=[], clues=[], triggers=[],
        )
        session = GameSession(
            module_id="m", current_scene_id="hall", world_state={}, status="active",
        )
        emma = Character(name="艾玛", rule_system="coc", base_attributes={}, skills={})
        henry = Character(name="亨利", rule_system="coc", base_attributes={}, skills={})
        msgs = build_team_context(
            emma, session, module, [], henry, all_teammates=[emma],
            team_guidance="本轮把话头多留给：亨利",
        )
        joined = "\n".join(m["content"] for m in msgs)
        assert "导演提示" in joined and "亨利" in joined

    def test_team_context空指引不注入(self):
        module = Module(
            title="t", rule_system="coc",
            scenes=[{"id": "hall", "name": "门厅", "description": "x"}],
            npcs=[], clues=[], triggers=[],
        )
        session = GameSession(
            module_id="m", current_scene_id="hall", world_state={}, status="active",
        )
        emma = Character(name="艾玛", rule_system="coc", base_attributes={}, skills={})
        henry = Character(name="亨利", rule_system="coc", base_attributes={}, skills={})
        msgs = build_team_context(emma, session, module, [], henry, all_teammates=[emma])
        assert not any("导演提示" in m["content"] for m in msgs)
