"""导演信号（节奏经营）纯函数单测，以及 direction 注入到 planner/team 上下文的接线测试。

信号是确定性启发式，全部不调 LLM。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
