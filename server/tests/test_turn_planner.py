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


def test_turn_plan_messages_apply_flag_resolved_npc_state(db_factory):
    """NPC 的位置/秘密可能因剧情 flag 变化（states 机制）。build_kp_context 会先按已激活
    flags 解析出『当前样貌』再喂给 KP；planner 必须看到同一份解析结果，否则会因为看着模组
    里的初始定义，把已经因剧情变化搬到别处、换了秘密的 NPC 判断错。"""
    db = db_factory()
    module = Module(
        title="状态测试",
        rule_system="coc",
        scenes=[
            {"id": "hall", "name": "门厅"},
            {"id": "study", "name": "书房"},
        ],
        npcs=[
            {
                "id": "butler",
                "name": "管家",
                "description": "老管家",
                "personality": "谦卑",
                "secrets": ["知道地下室入口"],
                "initial_location": "hall",
                "states": [
                    {
                        "when": ["butler_suspicious"],
                        "initial_location": "study",
                        "secrets": ["管家就是纵火者"],
                    }
                ],
            }
        ],
        clues=[],
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
        world_state={
            "visited_scenes": ["hall", "study"],
            "flags": ["butler_suspicious"],
        },
    )
    db.add(session)
    db.commit()

    messages = turn_planner.build_turn_plan_messages(
        session, module, hero, [], teammates=[], rules_lookup_enabled=False,
    )
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    npc = next(n for n in payload["visible_npcs"] if n["id"] == "butler")
    assert npc["location"] == "study"  # 因 flag 已搬到书房，不是模组里定义的门厅
    assert npc["secrets"] == ["管家就是纵火者"]  # 秘密也随 flag 更新，不是初始定义


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
    # 计划是内部工作稿：必须明确禁止把它的结构/字段名/内部 id 汇报体输出给玩家
    # （曾出现过 KP 把计划当成要念的报告，输出【场景状态更新】等标题+要点列表并泄露 flag 名）
    assert "内部工作稿" in injected["content"]
    assert "汇报体" in injected["content"]
    assert "do_not_reveal" in injected["content"]


def test_build_turn_plan_message_requires_check_硬约束():
    """requires_check=true 时，注入必须包含「照发的 [DICE_CHECK] 指令原文 + 不许提前泄结果」硬约束。

    这是评估回路里 plan_adherence 连续不过的根因修复：措辞太软时 KP 会把动作叙述
    「讲完」（敲出空层/摸到暗缝）却不发指令，既漏检定又提前泄露线索位置。
    """
    from app.ai.turn_planner import CheckPlan, TurnPlan

    plan = TurnPlan(
        requires_check=True,
        check=CheckPlan(skill="侦查", difficulty="normal", visibility="open"),
    )
    content = turn_planner.build_turn_plan_message(plan)["content"]
    # 必须把照发的指令原文喂给 KP（含技能名，open 明骰不赘写 visibility）
    assert "[DICE_CHECK: skill=侦查, difficulty=normal]" in content
    # 必须是「最后一行」硬约束，且明确禁止指令之前泄露结果/线索位置
    assert "最后一行" in content
    assert "凌驾叙事完整性" in content
    # 暗骰时 visibility 要写进指令原文
    blind_plan = TurnPlan(
        requires_check=True,
        check=CheckPlan(skill="心理学", difficulty="hard", visibility="blind"),
    )
    blind_content = turn_planner.build_turn_plan_message(blind_plan)["content"]
    assert "[DICE_CHECK: skill=心理学, difficulty=hard, visibility=blind]" in blind_content


def test_build_turn_plan_message_不需检定时无检定硬约束():
    """requires_check=false 时不注入检定硬约束，避免让 KP 无中生有地发检定。"""
    from app.ai.turn_planner import TurnPlan

    plan = TurnPlan(requires_check=False)
    content = turn_planner.build_turn_plan_message(plan)["content"]
    assert "本轮必须发起检定" not in content
    assert "[DICE_CHECK:" not in content


class _RawLLM:
    """按预设原始字符串/对象作 complete 返回值的桩 LLM。"""
    def __init__(self, raw):
        self._raw = raw

    async def complete(self, messages, temperature=0, response_format=None, max_tokens=None):
        return self._raw


@pytest.mark.asyncio
async def test_run_turn_planner_tolerates_dirty_json():
    """模型不严格遵守 JSON 约定时也要能解析（这直接关系 KP 裁定约束的稳定性）：
    ```json 围栏、前后夹解释文字、无语言标注围栏、以及已是 dict 的返回都应成功。"""
    valid = '{"turn_kind":"social","player_intent":"盘问管家"}'
    dirty = [
        "```json\n" + valid + "\n```",
        "这是本轮裁定计划：\n" + valid + "\n（以上）",
        "```\n" + valid + "\n```",
    ]
    for raw in dirty:
        plan = await turn_planner.run_turn_planner(_RawLLM(raw), [])
        assert plan is not None, raw
        assert plan.turn_kind == "social" and plan.player_intent == "盘问管家"

    # provider 已解析成 dict → 直接可用
    plan = await turn_planner.run_turn_planner(_RawLLM({"turn_kind": "combat"}), [])
    assert plan is not None and plan.turn_kind == "combat"


@pytest.mark.asyncio
async def test_run_turn_planner_fails_open_on_unparseable():
    """空/纯文本/无大括号 → 无法解析；schema 不符 → 校验失败。两者都回退为 None，不阻塞。"""
    for junk in ["", "抱歉，我无法生成计划", "no braces here"]:
        assert await turn_planner.run_turn_planner(_RawLLM(junk), []) is None
    # 合法 JSON 但 turn_kind 不在枚举内 → schema 校验失败 → None
    assert await turn_planner.run_turn_planner(_RawLLM('{"turn_kind":"invalid_kind"}'), []) is None
