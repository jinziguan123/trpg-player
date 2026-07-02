"""世界记忆层 v1（线索台账 + NPC 记忆）的单测：纯函数、确定性钩子与上下文注入。

不调 LLM：纯函数直接断言；带库的钩子用临时 SQLite（沿用 test_chat_service 的桩法）。
"""

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
