"""幕后推演（Backstage Clock）单测：触发条件、信息隔离、安全约束与 fail-open。

不调真实 LLM：全部用桩 provider（记录调用次数与消息、可指定返回/抛错）。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import turn_planner, turn_validator
from app.ai.agents import backstage_agent
from app.ai.context import build_kp_context, build_npc_context, build_team_context
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401 — 注册建表
from app.services import chat_service, session_service


# ── 桩 LLM ───────────────────────────────────────────────────────


class _StubLLM:
    """记录调用的桩 provider：payload 为返回值，exc 给定则抛出。"""

    def __init__(self, payload: str = "", exc: Exception | None = None):
        self.calls = 0
        self.messages: list[list[dict]] = []
        self.payload = payload
        self.exc = exc

    async def complete(self, messages, **kw):
        self.calls += 1
        self.messages.append(messages)
        if self.exc:
            raise self.exc
        return self.payload


_GOOD_JSON = json.dumps({
    "events": [{
        "npc_id": "npc_butler",
        "action": "把尸体从地窖移到井里",
        "affected_scene": "scene_well",
        "suggest_flags": ["flag_body_moved"],
    }],
}, ensure_ascii=False)


# ── 库夹具 ───────────────────────────────────────────────────────


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, with_secrets: bool = True):
    module = Module(
        title="幕后测试模组", rule_system="coc",
        scenes=[
            {"id": "scene_hall", "name": "大厅"},
            {"id": "scene_well", "name": "水井"},
        ],
        npcs=[{
            "id": "npc_butler", "name": "老管家",
            **({"secrets": "他谋杀了老爷，正设法转移证据"} if with_secrets else {}),
        }],
        triggers=[{"when": "尸体被发现", "set_flags": ["flag_body_found"]}],
    )
    player = Character(name="亨利", rule_system="coc")
    db.add_all([module, player])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=player.id,
        current_scene_id="scene_hall", status="active",
        world_state={"flags": ["flag_old"]},
    )
    db.add(session)
    db.commit()
    return session, module, player


def _add_player_turns(db, session, player, n: int):
    for i in range(n):
        session_service.add_event(
            db, session.id, "action", f"我调查大厅的第{i}个角落",
            actor_id=player.id, actor_name=player.name,
        )


def _run(db, session, llm):
    asyncio.run(chat_service._maybe_run_backstage(db, session.id, llm))


def _kp_events(db, session):
    return [
        e for e in session_service.get_session_events(db, session.id)
        if session_service.is_kp_only_event(e)
    ]


# ── 触发条件 ─────────────────────────────────────────────────────


def test_no_secret_npc_never_triggers(db_factory):
    db = db_factory()
    session, module, player = _seed(db, with_secrets=False)
    _add_player_turns(db, session, player, 8)
    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)
    assert llm.calls == 0                       # 零调用
    assert _kp_events(db, session) == []


def test_under_six_turns_not_triggered(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session.world_state = {
        "flags": ["flag_old"],
        "backstage": {"last_run_seq": 0, "last_scene_id": "scene_hall"},
    }
    db.commit()
    _add_player_turns(db, session, player, 3)
    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)
    assert llm.calls == 0
    assert _kp_events(db, session) == []
    # 游标未被推进
    assert session.world_state["backstage"]["last_run_seq"] == 0


def test_six_player_turns_trigger_and_persist(db_factory, monkeypatch):
    db = db_factory()
    session, module, player = _seed(db)
    _add_player_turns(db, session, player, 6)
    last_seq = session_service.get_session_events(db, session.id)[-1].sequence_num

    # 隔离 e：幕后事件生成绝不广播
    def _no_broadcast(*a, **kw):
        raise AssertionError("幕后推演不得向房间广播任何 chunk")
    monkeypatch.setattr(chat_service.room_hub, "broadcast", _no_broadcast)

    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)
    assert llm.calls == 1
    kp_events = _kp_events(db, session)
    assert len(kp_events) == 1
    ev = kp_events[0]
    assert ev.event_type == "system"
    assert ev.visibility == ["kp"]
    assert (ev.metadata_ or {}).get("kind") == "backstage"
    assert (ev.metadata_ or {}).get("npc_id") == "npc_butler"
    assert (ev.metadata_ or {}).get("suggest_flags") == ["flag_body_moved"]
    assert "老管家" in ev.content and "地窖" in ev.content
    assert "水井" in ev.content                  # affected_scene 渲染成场景名
    # 游标推进到评估时的最新事件序号（不含幕后事件自身的新序号）
    db.refresh(session)
    assert session.world_state["backstage"]["last_run_seq"] == last_seq


def test_scene_change_triggers_even_with_one_turn(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session.world_state = {
        "flags": ["flag_old"],
        "backstage": {"last_run_seq": 0, "last_scene_id": "scene_well"},
    }
    db.commit()
    _add_player_turns(db, session, player, 1)   # 远不足 6 回合
    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)                       # 当前场景 scene_hall ≠ 游标场景 scene_well
    assert llm.calls == 1
    assert len(_kp_events(db, session)) == 1


def test_first_evaluation_initializes_baseline_without_call(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    _add_player_turns(db, session, player, 1)
    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)
    assert llm.calls == 0                        # 首次只立基线，不触发
    db.refresh(session)
    bs = session.world_state.get("backstage") or {}
    assert bs.get("last_run_seq") == 0
    assert bs.get("last_scene_id") == "scene_hall"


# ── 安全约束：绝不直接改剧情状态 ─────────────────────────────────


def test_backstage_never_mutates_flags(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    _add_player_turns(db, session, player, 6)
    llm = _StubLLM(_GOOD_JSON)
    _run(db, session, llm)
    db.refresh(session)
    ws = session.world_state or {}
    assert ws.get("flags") == ["flag_old"]       # suggest_flags 只是建议，不落 flags
    assert "flag_body_moved" not in (ws.get("flags") or [])
    assert "clue_ledger" not in ws               # 也不碰台账等其他剧情状态


# ── fail-open ────────────────────────────────────────────────────


def test_llm_exception_fail_open(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session.world_state = {
        "flags": ["flag_old"],
        "backstage": {"last_run_seq": 0, "last_scene_id": "scene_hall"},
    }
    db.commit()
    _add_player_turns(db, session, player, 6)
    llm = _StubLLM(exc=RuntimeError("boom"))
    _run(db, session, llm)                        # 不上抛
    assert _kp_events(db, session) == []          # 无事件落库
    db.refresh(session)
    assert session.world_state["backstage"]["last_run_seq"] == 0  # 游标不动


def test_bad_json_fail_open(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session.world_state = {
        "flags": ["flag_old"],
        "backstage": {"last_run_seq": 0, "last_scene_id": "scene_hall"},
    }
    db.commit()
    _add_player_turns(db, session, player, 6)
    llm = _StubLLM("这不是 JSON {{{")
    _run(db, session, llm)
    assert _kp_events(db, session) == []
    db.refresh(session)
    assert session.world_state["backstage"]["last_run_seq"] == 0


def test_empty_events_still_advances_cursor(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session.world_state = {
        "flags": ["flag_old"],
        "backstage": {"last_run_seq": 0, "last_scene_id": "scene_hall"},
    }
    db.commit()
    _add_player_turns(db, session, player, 6)
    last_seq = session_service.get_session_events(db, session.id)[-1].sequence_num
    llm = _StubLLM(json.dumps({"events": []}))
    _run(db, session, llm)
    assert _kp_events(db, session) == []          # 无事发生也是合法结果
    db.refresh(session)
    # 游标照常推进——否则每轮都会重复触发推演
    assert session.world_state["backstage"]["last_run_seq"] == last_seq


# ── 输出解析（纯函数） ───────────────────────────────────────────


def test_parse_backstage_events():
    valid = {"npc_butler"}
    # 代码围栏 + 前后杂文容忍
    raw = "推演如下：\n```json\n" + _GOOD_JSON + "\n```"
    out = backstage_agent.parse_backstage_events(raw, valid)
    assert out and out[0]["npc_id"] == "npc_butler"
    # 幻觉 npc_id / 空 action 丢弃；最多 2 条
    raw = json.dumps({"events": [
        {"npc_id": "npc_ghost", "action": "不存在的人"},
        {"npc_id": "npc_butler", "action": ""},
        {"npc_id": "npc_butler", "action": "a1"},
        {"npc_id": "npc_butler", "action": "a2"},
        {"npc_id": "npc_butler", "action": "a3"},
    ]})
    out = backstage_agent.parse_backstage_events(raw, valid)
    assert [e["action"] for e in out] == ["a1", "a2"]
    # 坏 JSON → None（与「空事件列表」区分）
    assert backstage_agent.parse_backstage_events("oops", valid) is None
    assert backstage_agent.parse_backstage_events(json.dumps({"events": []}), valid) == []


def test_npcs_with_secrets_detection():
    m = Module(title="t", rule_system="coc", npcs=[
        {"id": "a", "name": "甲", "secrets": "有秘密"},
        {"id": "b", "name": "乙", "secrets": ""},
        {"id": "c", "name": "丙", "goals": ["夺回祖宅"]},
        {"id": "d", "name": "丁"},
    ])
    ids = [n["id"] for n in backstage_agent.npcs_with_secrets(m)]
    assert ids == ["a", "c"]
    assert backstage_agent.npcs_with_secrets(
        Module(title="t", rule_system="coc", npcs=[{"id": "d", "name": "丁"}]),
    ) == []


# ── 信息隔离：visibility=["kp"] 对各玩家侧出口全部不可见 ─────────


def _seed_with_backstage_event(db):
    """种一条幕后事件 + 一条普通旁白，返回 (session, module, player, kp_ev)。"""
    session, module, player = _seed(db)
    session_service.add_event(
        db, session.id, "narration", "大厅里烛火摇曳。", actor_name="KP",
    )
    kp_ev = session_service.add_event(
        db, session.id, "system", "老管家：把尸体从地窖移到井里（涉及：水井）",
        actor_name="幕后", visibility=["kp"],
        metadata={"kind": "backstage", "npc_id": "npc_butler",
                  "affected_scene": "scene_well", "suggest_flags": ["flag_body_moved"]},
    )
    return session, module, player, kp_ev


def test_isolation_a_latest_events_pagination(db_factory):
    """出口 a：前端历史/重连（get_latest_events → GET /{id}/events）不返回幕后事件。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    events, _ = session_service.get_latest_events(db, session.id, limit=50)
    ids = {e.id for e in events}
    assert kp_ev.id not in ids
    assert any(e.event_type == "narration" for e in events)  # 普通事件照常返回


def test_isolation_b_search_history(db_factory):
    """出口 b：历史搜索不命中幕后事件。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    hits = session_service.search_events(db, session.id, "地窖")
    assert hits == []
    # 普通事件照常可搜
    assert any("烛火" in e.content for e in session_service.search_events(db, session.id, "烛火"))


def test_isolation_c_team_context(db_factory):
    """出口 c：AI 队友（玩家侧）上下文摘要绝不含幕后事件。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    mate = Character(name="约翰", rule_system="coc")
    db.add(mate)
    db.commit()
    events = session_service.get_session_events(db, session.id)
    assert any(session_service.is_kp_only_event(e) for e in events)  # 原始流里确实有
    messages = build_team_context(mate, session, module, events, player)
    joined = "\n".join(m["content"] for m in messages)
    assert "地窖" not in joined and "尸体" not in joined
    assert "烛火" in joined                       # 普通旁白照常进摘要


def test_team_context_不泄漏场景作者描述(db_factory):
    """AI 队友（玩家侧）不得看到模组场景的作者视角 description/keywords——那含玩家尚未发现的
    细节（门上的便签等）；泄露即队友会「主动」抖出 KP 还没揭示的线索（线上复现的 bug）。"""
    db = db_factory()
    module = Module(
        title="常暗", rule_system="coc", npcs=[],
        scenes=[{"id": "scene_1", "name": "6号车厢",
                 "description": "车厢门上贴着一张便签，门旁有电车示意图。",
                 "keywords": ["便签", "电车示意图"]}],
    )
    player = Character(name="龙牙", rule_system="coc")
    mate = Character(name="直树", rule_system="coc")
    db.add_all([module, player, mate]); db.flush()
    session = GameSession(module_id=module.id, player_character_id=player.id,
                          current_scene_id="scene_1", status="active", world_state={})
    db.add(session); db.commit()
    # KP 只叙述了车门与示意图，没提便签
    session_service.add_event(db, session.id, "narration",
                              "车厢空无一人，门旁固定着电车示意图。", actor_name="KP")
    events = session_service.get_session_events(db, session.id)
    joined = "\n".join(m["content"] for m in build_team_context(mate, session, module, events, player))
    assert "便签" not in joined                   # 作者描述里未揭示的细节绝不泄露
    assert "6号车厢" in joined                    # 位置名（current_location）照常给
    assert "电车示意图" in joined                  # KP 已叙述的内容经事件摘要照常可见


def test_isolation_d_npc_context(db_factory):
    """出口 d：NPC 上下文的 _npc_can_see 天然排除 visibility=["kp"]（锁死语义）。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    # 再种一条「非 system 类型」的 kp 事件，确保排除靠的是 visibility 语义本身，
    # 而不是 _events_to_messages 跳过 system 的巧合。
    session_service.add_event(
        db, session.id, "narration", "（幕后专供旁白）地窖里传来拖拽声。",
        actor_name="幕后", visibility=["kp"], metadata={"kind": "backstage"},
    )
    events = session_service.get_session_events(db, session.id)
    messages = build_npc_context("npc_butler", session, module, events)
    joined = "\n".join(m["content"] for m in messages)
    assert "拖拽声" not in joined
    assert "把尸体从地窖移到井里" not in joined


def test_kp_context_gets_backstage_section(db_factory):
    """唯一出口：KP 上下文注入「幕后动态」小节（带守密措辞与 suggest_flags 建议）。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    events = session_service.get_session_events(db, session.id)
    messages = build_kp_context(session, module, player, events)
    system = messages[0]["content"]
    assert "幕后动态" in system
    assert "把尸体从地窖移到井里" in system
    assert "玩家不可见" in system and "绝不直接复述" in system
    assert "flag_body_moved" in system            # suggest_flags 作为建议给 KP
    # 幕后事件不重复混入对话消息流（system 事件本就不进 _events_to_messages）
    tail = "\n".join(m["content"] for m in messages[1:])
    assert "地窖" not in tail


def test_kp_context_without_backstage_unchanged(db_factory):
    db = db_factory()
    session, module, player = _seed(db)
    session_service.add_event(db, session.id, "narration", "大厅里烛火摇曳。", actor_name="KP")
    events = session_service.get_session_events(db, session.id)
    messages = build_kp_context(session, module, player, events)
    assert "幕后动态" not in messages[0]["content"]  # 无幕后事件不注入（向后兼容）


def test_isolation_f_story_summary_excludes_backstage(db_factory):
    """出口 f：滚动剧情摘要的浓缩输入不含幕后事件（摘要注入所有后续 KP 上下文）。"""
    db = db_factory()
    session, module, player, kp_ev = _seed_with_backstage_event(db)
    # 攒够触发阈值的普通事件
    for i in range(chat_service.STORY_SUMMARY_TRIGGER + chat_service.STORY_SUMMARY_KEEP_RECENT + 2):
        session_service.add_event(
            db, session.id, "narration", f"剧情推进第{i}拍。", actor_name="KP",
        )
    llm = _StubLLM("")                            # 摘要失败静默，无所谓返回值
    asyncio.run(chat_service._maybe_roll_story_summary(db, session.id, llm))
    assert llm.calls == 1                         # 确认走到了浓缩调用
    prompt = json.dumps([str(m) for m in llm.messages[0]], ensure_ascii=False)
    assert "地窖" not in prompt and "尸体" not in prompt
    assert "剧情推进第0拍" in prompt              # 普通事件照常进浓缩输入


# ── validator 预筛 ───────────────────────────────────────────────


def test_augment_plan_do_not_reveal_and_prescreen():
    plan = turn_planner.TurnPlan()
    assert turn_validator._looks_suspicious("平静的旁白。", plan) is False
    ev = EventLog(
        session_id="s", sequence_num=9, event_type="system",
        content="老管家：把尸体从地窖移到井里", visibility=["kp"],
        metadata_={"kind": "backstage"},
    )
    chat_service._augment_plan_with_backstage(plan, [ev])
    assert any("地窖" in t for t in plan.safety.do_not_reveal)
    # do_not_reveal 非空 → 预筛判定值得校验，KP 复述幕后文本会被校验器拦下改写
    assert turn_validator._looks_suspicious("平静的旁白。", plan) is True
    # 幂等：重复挂载不重复追加
    chat_service._augment_plan_with_backstage(plan, [ev])
    assert len(plan.safety.do_not_reveal) == 1


def test_augment_plan_none_and_empty_noop():
    chat_service._augment_plan_with_backstage(None, [])  # 不抛
    plan = turn_planner.TurnPlan()
    chat_service._augment_plan_with_backstage(plan, [])
    assert plan.safety.do_not_reveal == []


def test_backstage_payload_includes_truth(db_factory):
    """幕后推演 payload 带模组幕后真相：NPC 的小步动作有全局总纲可循。"""
    db = db_factory()
    session, module, _player = _seed(db)
    module.truth = "老爷之死是管家与账房合谋，证据藏在水井。"
    db.commit()
    messages = backstage_agent.build_backstage_messages(
        session, module, backstage_agent.npcs_with_secrets(module), [],
    )
    assert "证据藏在水井" in messages[1]["content"]
