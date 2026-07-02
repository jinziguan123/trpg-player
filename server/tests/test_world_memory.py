"""世界记忆层 v1（线索台账 + NPC 记忆）的单测：纯函数、确定性钩子与上下文注入。

不调 LLM：纯函数直接断言；带库的钩子用临时 SQLite（沿用 test_chat_service 的桩法）。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import story_summarizer, turn_planner
from app.ai.context import build_kp_context, build_npc_context
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401 — 注册建表
from app.services import chat_service, world_memory


# ── 纯函数：台账写入 ──────────────────────────────────────────────


def test_record_clue_reveal_levels_and_merge():
    ws = world_memory.record_clue_reveal(
        {}, ["clue_key"], "hint", ["char_a"], 10, note="在书桌暗格附近有所察觉",
    )
    entry = ws["clue_ledger"]["clue_key"]
    assert entry["status"] == "partial"          # hint → partial
    assert entry["discovered_by"] == ["char_a"]
    assert entry["seq"] == 10

    # direct → known 升级；discovered_by 增量合并去重；seq 保留首次触碰值
    ws = world_memory.record_clue_reveal(ws, ["clue_key"], "direct", ["char_a", "char_b"], 20)
    entry = ws["clue_ledger"]["clue_key"]
    assert entry["status"] == "known"
    assert entry["discovered_by"] == ["char_a", "char_b"]
    assert entry["seq"] == 10

    # known 不降级：后续 hint 不会退回 partial
    ws = world_memory.record_clue_reveal(ws, ["clue_key"], "hint", ["char_c"], 30)
    assert ws["clue_ledger"]["clue_key"]["status"] == "known"


def test_record_clue_reveal_none_is_noop():
    ws = {"flags": {"x": True}}
    out = world_memory.record_clue_reveal(ws, ["clue_key"], "none", ["char_a"], 1)
    assert "clue_ledger" not in out
    out = world_memory.record_clue_reveal(ws, [], "direct", ["char_a"], 1)
    assert "clue_ledger" not in out


def test_record_clue_reveal_does_not_mutate_input():
    ws = {"clue_ledger": {"old": {"status": "partial"}}}
    world_memory.record_clue_reveal(ws, ["new"], "direct", ["char_a"], 5)
    assert "new" not in ws["clue_ledger"]  # 入参不被就地修改（读-改-写返回新 dict）


def test_discovered_clue_status():
    ws = {"clue_ledger": {
        "a": {"status": "known"}, "b": {"status": "partial"}, "c": {"status": "weird"},
    }}
    assert world_memory.discovered_clue_status(ws) == {"a": "known", "b": "partial"}
    assert world_memory.discovered_clue_status({}) == {}


# ── 纯函数：NPC 互动环形缓冲 ─────────────────────────────────────


def test_npc_interactions_ring_buffer_cap():
    ws = {}
    for i in range(12):
        ws = world_memory.record_npc_interaction(ws, "npc_butler", i, f"互动{i}")
    interactions = ws["npc_memory"]["npc_butler"]["interactions"]
    assert len(interactions) == world_memory.MAX_NPC_INTERACTIONS  # 上限 8
    assert interactions[0]["summary"] == "互动4"   # 最老的被挤出
    assert interactions[-1]["summary"] == "互动11"


def test_npc_interaction_preserves_other_fields():
    ws = {"npc_memory": {"npc_butler": {
        "attitude": "warming", "promises": ["答应半夜带玩家看西厢房"],
    }}}
    ws = world_memory.record_npc_interaction(ws, "npc_butler", 5, "被看穿慌张")
    entry = ws["npc_memory"]["npc_butler"]
    assert entry["attitude"] == "warming"          # 追加互动不冲掉既有字段
    assert entry["promises"] == ["答应半夜带玩家看西厢房"]
    assert entry["interactions"][-1]["summary"] == "被看穿慌张"


# ── 上下文注入 ───────────────────────────────────────────────


def _mem_module() -> Module:
    return Module(
        title="记忆测试模组", rule_system="coc",
        scenes=[{"id": "scene_hall", "name": "大厅"}],
        npcs=[{"id": "npc_butler", "name": "老管家", "initial_location": "scene_hall"}],
        clues=[{"id": "clue_key", "name": "书房钥匙", "location": "scene_hall"}],
    )


def _mem_session(world_state: dict) -> GameSession:
    s = GameSession(
        module_id="m1", player_character_id="char_a",
        current_scene_id="scene_hall", status="active", world_state=world_state,
    )
    return s


def _mem_char() -> Character:
    c = Character(name="亨利", rule_system="coc")
    c.id = "char_a"
    return c


def _one_event() -> list[EventLog]:
    return [EventLog(
        session_id="s1", sequence_num=1, event_type="action",
        actor_id="char_a", actor_name="亨利", content="我检查书桌",
    )]


def test_kp_context_contains_clue_ledger_and_npc_memory():
    ws = {
        "visited_scenes": ["scene_hall"],
        "clue_ledger": {"clue_key": {
            "status": "known", "discovered_by": ["char_a"], "seq": 3, "note": "暗格里找到",
        }},
        "npc_memory": {"npc_butler": {
            "attitude": "warming",
            "promises": ["答应半夜带玩家看西厢房"],
            "lies_told": ["谎称老爷死时自己在厨房"],
            "interactions": [{"seq": 2, "summary": "被亨利用心理学看穿慌张"}],
        }},
    }
    messages = build_kp_context(_mem_session(ws), _mem_module(), _mem_char(), _one_event())
    system = messages[0]["content"]
    assert "线索台账" in system
    assert "书房钥匙" in system and "完全掌握" in system
    assert "亨利" in system                       # discovered_by 的 id 已映射成角色名
    assert "不要重复安排「发现」桥段" in system
    assert "未列出的线索一律视为未发现" in system
    assert "NPC 记忆" in system
    assert "答应半夜带玩家看西厢房" in system
    assert "谎称老爷死时自己在厨房" in system


def test_kp_context_empty_memory_backward_compatible():
    ws = {"visited_scenes": ["scene_hall"]}
    messages = build_kp_context(_mem_session(ws), _mem_module(), _mem_char(), _one_event())
    system = messages[0]["content"]
    assert "线索台账" not in system   # 空台账不注入任何小节，与现状一致
    assert "NPC 记忆" not in system


def test_kp_context_opening_never_injects_ledger():
    ws = {"clue_ledger": {"clue_key": {"status": "known"}}}
    messages = build_kp_context(_mem_session(ws), _mem_module(), _mem_char(), [])
    assert "线索台账" not in messages[0]["content"]  # 开场隔离照旧


def test_npc_context_injects_own_memory():
    ws = {"npc_memory": {"npc_butler": {
        "attitude": "wary",
        "promises": ["答应半夜带玩家看西厢房"],
        "lies_told": ["谎称老爷死时自己在厨房"],
        "interactions": [{"seq": 2, "summary": "被亨利用心理学看穿慌张"}],
    }}}
    messages = build_npc_context("npc_butler", _mem_session(ws), _mem_module(), [])
    system = messages[0]["content"]
    assert "你的记忆" in system
    assert "答应半夜带玩家看西厢房" in system
    assert "谎称老爷死时自己在厨房" in system
    assert "被亨利用心理学看穿慌张" in system


def test_npc_context_without_memory_unchanged():
    messages = build_npc_context("npc_butler", _mem_session({}), _mem_module(), [])
    assert "你的记忆" not in messages[0]["content"]


def test_turn_planner_marks_ledger_clues_discovered():
    ws = {
        "visited_scenes": ["scene_hall"],
        "clue_ledger": {"clue_key": {"status": "known", "discovered_by": ["char_a"]}},
    }
    messages = turn_planner.build_turn_plan_messages(
        _mem_session(ws), _mem_module(), _mem_char(), _one_event(),
    )
    user = messages[1]["content"]
    assert "不得再进入 candidate_clue_ids" in user
    payload = json.loads(user[user.index("{"):])
    assert payload["clue_ledger"] == {"clue_key": "known"}
    clue = next(c for c in payload["visible_clues"] if c["id"] == "clue_key")
    assert clue["discovered"] is True             # 台账 known → 已发现，不再是 candidate


def test_turn_planner_empty_ledger_backward_compatible():
    messages = turn_planner.build_turn_plan_messages(
        _mem_session({"visited_scenes": ["scene_hall"]}), _mem_module(), _mem_char(),
        _one_event(),
    )
    payload = json.loads(messages[1]["content"][messages[1]["content"].index("{"):])
    assert payload["clue_ledger"] == {}
    clue = next(c for c in payload["visible_clues"] if c["id"] == "clue_key")
    assert clue["discovered"] is False


def test_story_summary_prompt_mentions_ledger():
    messages = story_summarizer.build_summary_messages(
        "", [EventLog(session_id="s", sequence_num=1, event_type="narration", content="x")],
    )
    assert "台账" in messages[1]["content"]
    assert "剧情脉络" in messages[1]["content"]


# ── chat_service 确定性钩子（带库） ─────────────────────────────


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="记忆测试模组", rule_system="coc",
        npcs=[{"id": "npc_butler", "name": "老管家"}],
        clues=[{"id": "clue_key", "name": "书房钥匙"}],
    )
    player = Character(name="亨利", rule_system="coc")
    mate = Character(name="约翰", rule_system="coc")
    db.add_all([module, player, mate])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=player.id, status="active",
        world_state={},
    )
    db.add(session)
    db.commit()
    return session, module, player, mate


def test_record_clue_ledger_from_plan_hook(db_factory):
    db = db_factory()
    session, module, player, mate = _seed(db)
    plan = turn_planner.TurnPlan(
        clue_policy=turn_planner.CluePolicy(
            action_matches_clue=True,
            candidate_clue_ids=["clue_key"],
            reveal_level="direct",
            notes="书桌暗格已被打开",
        ),
    )
    events = [EventLog(
        session_id=session.id, sequence_num=7, event_type="action",
        actor_id=player.id, actor_name=player.name, content="我撬开暗格",
    )]
    chat_service._record_clue_ledger_from_plan(
        db, session, plan, events, player, [mate],
    )
    entry = (session.world_state or {}).get("clue_ledger", {}).get("clue_key")
    assert entry is not None
    assert entry["status"] == "known"             # direct → known
    assert player.id in entry["discovered_by"]    # 同场景队友一并在场
    assert mate.id in entry["discovered_by"]
    assert entry["seq"] == 7
    assert "书桌暗格" in entry["note"]


def test_record_clue_ledger_from_plan_none_level_noop(db_factory):
    db = db_factory()
    session, module, player, mate = _seed(db)
    plan = turn_planner.TurnPlan(
        clue_policy=turn_planner.CluePolicy(
            candidate_clue_ids=["clue_key"], reveal_level="none",
        ),
    )
    chat_service._record_clue_ledger_from_plan(db, session, plan, [], player, [mate])
    assert not (session.world_state or {}).get("clue_ledger")


def test_record_npc_say_memory_hook(db_factory):
    db = db_factory()
    session, module, player, mate = _seed(db)
    extracted = [
        ("老管家", "老爷死时我在厨房，什么都没看见。"),
        ("老管家", "你们还是快些离开吧。"),      # 同一 NPC 一轮只记一条
        ("约翰", "我不信。"),                     # 队友台词不入 NPC 记忆
    ]
    chat_service._record_npc_say_memory(
        db, session.id, session, module, extracted, [player.name, mate.name],
    )
    memory = (session.world_state or {}).get("npc_memory", {})
    assert list(memory.keys()) == ["npc_butler"]
    interactions = memory["npc_butler"]["interactions"]
    assert len(interactions) == 1
    assert "对亨利、约翰说" in interactions[0]["summary"]
    assert "厨房" in interactions[0]["summary"]


def test_match_single_npc_requires_unique_hit():
    module = Module(title="t", rule_system="coc", npcs=[
        {"id": "npc_a", "name": "老管家"},
        {"id": "npc_b", "name": "女仆安娜"},
    ])
    assert chat_service._match_single_npc(module, "我用心理学观察老管家的神色") == (
        "npc_a", "老管家",
    )
    # 多命中 / 零命中：归属不成立，跳过
    assert chat_service._match_single_npc(module, "观察老管家和女仆安娜") is None
    assert chat_service._match_single_npc(module, "观察周围环境") is None
    assert chat_service._match_single_npc(module, "") is None


def test_apply_world_memory_fail_open(db_factory):
    db = db_factory()
    session, *_ = _seed(db)

    def _boom(ws):
        raise RuntimeError("boom")

    # 更新函数抛异常不得上抛（fail-open），world_state 保持原样
    chat_service._apply_world_memory(db, session, _boom)
    assert (session.world_state or {}) == {}


# ── v2：MemoryKeeper 差量合并（纯函数）─────────────────────────────


def _mem_ws() -> dict:
    return {
        "clue_ledger": {"clue_key": {"status": "partial", "note": "旧备注"}},
        "npc_memory": {"npc_butler": {
            "attitude": "neutral",
            "promises": ["答应带路"],
            "lies_told": ["谎称在厨房"],
            "interactions": [{"seq": 1, "summary": "初次照面"}],
        }},
    }


def test_apply_memory_delta_updates_attitude_and_appends():
    ws = world_memory.apply_memory_delta(
        _mem_ws(),
        npc_updates={"npc_butler": {
            "attitude": "wary",
            "attitude_reason": "被追问后神色慌张",
            "new_promises": ["答应带路", "答应半夜开西厢房门"],  # 前者已存在→去重
            "new_lies": ["谎称没听到动静"],
        }},
        clue_notes={"clue_key": "意识到暗格对应地下室"},
    )
    npc = ws["npc_memory"]["npc_butler"]
    assert npc["attitude"] == "wary"
    assert npc["attitude_reason"] == "被追问后神色慌张"
    assert npc["promises"] == ["答应带路", "答应半夜开西厢房门"]   # 追加去重保序
    assert npc["lies_told"] == ["谎称在厨房", "谎称没听到动静"]
    assert npc["interactions"] == [{"seq": 1, "summary": "初次照面"}]  # 环形缓冲不被触碰
    assert ws["clue_ledger"]["clue_key"]["note"] == "意识到暗格对应地下室"
    assert ws["clue_ledger"]["clue_key"]["status"] == "partial"       # status 恒不变


def test_apply_memory_delta_does_not_mutate_input():
    src = _mem_ws()
    world_memory.apply_memory_delta(
        src, npc_updates={"npc_butler": {"new_lies": ["新谎"]}},
    )
    assert src["npc_memory"]["npc_butler"]["lies_told"] == ["谎称在厨房"]


def test_apply_memory_delta_rejects_bad_attitude():
    ws = world_memory.apply_memory_delta(
        _mem_ws(), npc_updates={"npc_butler": {"attitude": "furious"}},
    )
    # 枚举外的态度视为幻觉丢弃，保持原态度
    assert ws["npc_memory"]["npc_butler"]["attitude"] == "neutral"


# ── v2：安全约束——台账 status 与不存在实体 ─────────────────────


def test_apply_memory_delta_ignores_clue_status_tampering():
    # 抽取器即便在 clue_notes 里塞 status/discovered，也只取 note，绝不改状态
    ws = world_memory.apply_memory_delta(
        _mem_ws(),
        clue_notes={"clue_key": {"status": "known", "note": "玩家已完全掌握"}},
    )
    assert ws["clue_ledger"]["clue_key"]["status"] == "partial"       # status 不变
    # value 是 dict（非字符串备注）经 _truncate 转成其 str 形态，仍不触碰 status 键
    assert "known" not in ws["clue_ledger"]["clue_key"].get("status", "")


def test_apply_memory_delta_ignores_unknown_npc_and_clue():
    ws = world_memory.apply_memory_delta(
        _mem_ws(),
        npc_updates={"npc_ghost": {"attitude": "hostile", "new_lies": ["瞎编"]}},
        clue_notes={"clue_nonexistent": "凭空线索"},
    )
    assert "npc_ghost" not in ws["npc_memory"]        # 不存在的 NPC 不新建
    assert "clue_nonexistent" not in ws["clue_ledger"]  # 不存在的线索不新建
    # 已存在实体不受牵连
    assert ws["npc_memory"]["npc_butler"]["attitude"] == "neutral"


# ── v2：合并调用形态（假 provider 桩，不调真实 LLM）──────────────


class _FakeLLM:
    """桩：complete 返回预置字符串或抛异常，记录最后一次调用参数。"""

    def __init__(self, resp=None, boom=False):
        self.resp = resp
        self.boom = boom
        self.last_kw = None

    async def complete(self, messages, temperature=0.7, **kw):
        self.last_kw = {"temperature": temperature, **kw}
        if self.boom:
            raise RuntimeError("provider down")
        return self.resp


def _summ_events() -> list[EventLog]:
    return [EventLog(
        session_id="s", sequence_num=1, event_type="narration", content="管家闪烁其词。",
    )]


def test_summarize_and_extract_merged_shape():
    llm = _FakeLLM(json.dumps({
        "summary": "调查者审讯管家，他前后矛盾。",
        "npc_updates": {"npc_butler": {"attitude": "wary", "new_lies": ["谎称没出门"]}},
        "clue_notes": {"clue_key": "管家回避提及钥匙"},
    }))
    got = asyncio.run(story_summarizer.summarize_and_extract(
        llm, "既往摘要", _summ_events(), "- npc_butler：态度：中立",
    ))
    assert got is not None
    summary, npc_updates, clue_notes = got
    assert "审讯管家" in summary                       # 摘要文本正确产出，未因抽取回归
    assert npc_updates["npc_butler"]["attitude"] == "wary"
    assert clue_notes == {"clue_key": "管家回避提及钥匙"}
    # 合并调用是一次低温 json_object 调用
    assert llm.last_kw["temperature"] == 0
    assert llm.last_kw["response_format"] == {"type": "json_object"}


def test_summarize_and_extract_fail_open_on_exception():
    llm = _FakeLLM(boom=True)
    got = asyncio.run(story_summarizer.summarize_and_extract(
        llm, "既往", _summ_events(), "",
    ))
    assert got is None


def test_summarize_and_extract_fail_open_on_bad_json():
    llm = _FakeLLM("这不是 JSON，只是一段闲聊。")
    got = asyncio.run(story_summarizer.summarize_and_extract(
        llm, "既往", _summ_events(), "",
    ))
    assert got is None


def test_summarize_and_extract_empty_summary_is_none():
    llm = _FakeLLM(json.dumps({"summary": "", "npc_updates": {}, "clue_notes": {}}))
    got = asyncio.run(story_summarizer.summarize_and_extract(
        llm, "既往", _summ_events(), "",
    ))
    assert got is None


# ── v2：接线（_maybe_roll_story_summary 合并落库，带库）──────────


def _seed_long_session(db, n_events: int):
    """建一个已攒够摘要阈值的会话：n 条 narration 事件 + 一个 NPC 记忆种子。"""
    module = Module(
        title="记忆测试模组", rule_system="coc",
        npcs=[{"id": "npc_butler", "name": "老管家"}],
        clues=[{"id": "clue_key", "name": "书房钥匙"}],
    )
    player = Character(name="亨利", rule_system="coc")
    db.add_all([module, player])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=player.id, status="active",
        world_state={"npc_memory": {"npc_butler": {"attitude": "neutral"}},
                     "clue_ledger": {"clue_key": {"status": "partial"}}},
    )
    db.add(session)
    db.flush()
    for i in range(1, n_events + 1):
        db.add(EventLog(
            session_id=session.id, sequence_num=i, event_type="narration",
            content=f"第{i}段旁白。",
        ))
    db.commit()
    return session


def test_maybe_roll_story_summary_applies_delta(db_factory):
    db = db_factory()
    # TRIGGER=24：需 >24 条未并入事件才触发
    session = _seed_long_session(db, chat_service.STORY_SUMMARY_TRIGGER + 5)
    llm = _FakeLLM(json.dumps({
        "summary": "剧情梗概正文。",
        "npc_updates": {"npc_butler": {"attitude": "wary",
                                       "new_promises": ["答应带路"]}},
        "clue_notes": {"clue_key": "补充备注"},
    }))
    asyncio.run(chat_service._maybe_roll_story_summary(db, session.id, llm))
    ws = session.world_state or {}
    assert ws.get("story_summary") == "剧情梗概正文。"           # 摘要落库
    assert ws["story_summary_seq"] > 0                          # 游标推进
    assert ws["npc_memory"]["npc_butler"]["attitude"] == "wary"  # 差量合并
    assert ws["npc_memory"]["npc_butler"]["promises"] == ["答应带路"]
    assert ws["clue_ledger"]["clue_key"]["note"] == "补充备注"
    assert ws["clue_ledger"]["clue_key"]["status"] == "partial"  # status 恒不变


def test_maybe_roll_story_summary_fail_open_keeps_memory(db_factory):
    db = db_factory()
    session = _seed_long_session(db, chat_service.STORY_SUMMARY_TRIGGER + 5)
    before = dict(session.world_state or {})
    asyncio.run(chat_service._maybe_roll_story_summary(db, session.id, _FakeLLM(boom=True)))
    ws = session.world_state or {}
    # provider 抛异常：摘要不推进、NPC 记忆原样不变
    assert "story_summary" not in ws
    assert ws["npc_memory"] == before["npc_memory"]


def test_maybe_roll_story_summary_below_threshold_noop(db_factory):
    db = db_factory()
    session = _seed_long_session(db, 3)   # 远不够阈值
    called = {"n": 0}

    class _Counting(_FakeLLM):
        async def complete(self, messages, temperature=0.7, **kw):
            called["n"] += 1
            return await super().complete(messages, temperature, **kw)

    asyncio.run(chat_service._maybe_roll_story_summary(db, session.id, _Counting("{}")))
    assert called["n"] == 0                # 未攒够阈值：零 LLM 调用
    assert "story_summary" not in (session.world_state or {})
