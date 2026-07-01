"""回合规划器的回归测试。

只验证结构化规划层，不依赖真实 LLM。
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import turn_planner
from app.models import Base, Character, EventLog, GameSession, Module  # noqa: F401


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
        title="规划测试",
        rule_system="coc",
        scenes=[
            {"id": "hall", "name": "门厅", "description": "昏暗门厅"},
            {"id": "study", "name": "书房", "description": "尘封书房"},
        ],
        npcs=[
            {
                "id": "butler",
                "name": "管家",
                "description": "老管家",
                "personality": "谦卑",
                "secrets": ["知道地下室入口"],
                "initial_location": "hall",
            }
        ],
        clues=[
            {
                "id": "c1",
                "name": "书桌暗格",
                "description": "书桌内侧有一块松动的木板",
                "location": "study",
                "trigger_condition": "搜查书桌",
                "discovered": False,
            },
            {
                "id": "c2",
                "name": "地下室手记",
                "description": "被布遮住的手记",
                "location": "basement",
                "trigger_condition": "进入地下室",
                "discovered": False,
            },
        ],
        world_setting={},
    )
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id,
        player_character_id=hero.id,
        status="active",
        current_scene_id="study",
        world_state={"visited_scenes": ["hall", "study"]},
    )
    db.add(session)
    db.commit()
    return module, hero, session


def test_turn_plan_messages_include_trigger_condition(db_factory):
    db = db_factory()
    module, hero, session = _seed(db)
    messages = turn_planner.build_turn_plan_messages(
        session,
        module,
        hero,
        [],
        teammates=[],
        rules_lookup_enabled=False,
    )
    text = "\n".join(m["content"] for m in messages)
    assert "搜查书桌" in text
    assert "书桌暗格" in text
    assert "地下室手记" not in text


def test_turn_plan_messages_include_recent_actor_names(db_factory):
    db = db_factory()
    module, hero, session = _seed(db)
    event = EventLog(
        session_id=session.id,
        sequence_num=1,
        event_type="action",
        actor_id=hero.id,
        actor_name="调查员",
        content="我搜查书桌",
    )
    messages = turn_planner.build_turn_plan_messages(
        session,
        module,
        hero,
        [event],
        teammates=[],
        rules_lookup_enabled=False,
    )
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["recent_events"][0]["speaker"] == "调查员"
    assert payload["recent_events"][0]["content"] == "我搜查书桌"


@pytest.mark.asyncio
async def test_run_turn_planner_parses_json_plan():
    class _FakeLLM:
        async def complete(self, messages, temperature=0, response_format=None, max_tokens=None):
            return (
                '{"turn_kind":"investigate","player_intent":"调查书桌","requires_check":true,'
                '"check":{"skill":"侦查","difficulty":"normal","visibility":"open","reason":"结果不确定"},'
                '"clue_policy":{"action_matches_clue":true,"candidate_clue_ids":["c1"],"reveal_level":"basic",'
                '"requires_inspiration":false,"notes":"成功后给出暗格线索"},'
                '"npc_policy":{"speakers":["butler"],"reaction":"管家警觉","needs_npc_act":false},'
                '"scene_policy":{"scene_change":null,"set_flags":[],"clear_flags":[]},'
                '"narration_brief":["描述搜查动作","让管家插话阻拦"],'
                '"safety":{"do_not_reveal":["管家秘密"],"do_not_control_players":true}}'
            )

    messages = [{"role": "user", "content": "玩家正在搜查书桌"}]
    plan = await turn_planner.run_turn_planner(_FakeLLM(), messages)
    assert plan is not None
    assert plan.turn_kind == "investigate"
    assert plan.check.skill == "侦查"
    assert plan.clue_policy.candidate_clue_ids == ["c1"]
    injected = turn_planner.build_turn_plan_message(plan)
    assert injected["role"] == "system"
    assert "调查书桌" in injected["content"]
    assert "管家秘密" in injected["content"]
