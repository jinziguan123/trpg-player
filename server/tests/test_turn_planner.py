"""回合规划器的回归测试。

只验证结构化规划层，不依赖真实 LLM。
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import turn_planner
from app.models import Base, Character, EventLog, GameSession, Module  # noqa: F401


def _payload(messages) -> dict:
    """从规划器 user 消息里稳健地抠出 payload JSON（不依赖指令文本里的换行数量）。"""
    content = messages[1]["content"]
    start = content.index("{")
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(content[start:i + 1])
    raise AssertionError("payload JSON 未找到")


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


def test_turn_plan_messages_include_characteristic_and_unstuck_hint(db_factory):
    """planner 指令须告知：check.skill 可用九维属性中文名；卡关时主动裁定灵感/教育检定解卡。"""
    db = db_factory()
    module, hero, session = _seed(db)
    messages = turn_planner.build_turn_plan_messages(
        session, module, hero, [], teammates=[], rules_lookup_enabled=False,
    )
    instruction = messages[1]["content"]
    assert "九维属性中文名" in instruction
    assert "灵感=智力" in instruction
    assert "卡关" in instruction and "解卡" in instruction
    assert "direction.nudge" in instruction


def test_turn_plan_messages_require_structured_combat_decision(db_factory):
    """规划器必须明确区分普通动作与开战，并返回可执行的敌方名单。"""
    db = db_factory()
    module, hero, session = _seed(db)
    messages = turn_planner.build_turn_plan_messages(
        session, module, hero, [], teammates=[], rules_lookup_enabled=False,
    )
    instruction = messages[1]["content"]
    assert "combat.should_start" in instruction
    assert "结构化战斗" in instruction
    assert "enemies" in instruction


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
    payload = _payload(messages)
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
    payload = _payload(messages)
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
                '"combat":{"should_start":false,"enemies":[],"trigger":""},'
                '"narration_brief":["描述搜查动作","让管家插话阻拦"],'
                '"safety":{"do_not_reveal":["管家秘密"],"do_not_control_players":true}}'
            )

    messages = [{"role": "user", "content": "玩家正在搜查书桌"}]
    plan = await turn_planner.run_turn_planner(_FakeLLM(), messages)
    assert plan is not None
    assert plan.turn_kind == "investigate"
    assert plan.check.skill == "侦查"
    assert plan.clue_policy.candidate_clue_ids == ["c1"]
    assert plan.combat.should_start is False
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


def test_build_turn_plan_message_carries_group_check_scope():
    from app.ai.turn_planner import CheckPlan, TurnPlan

    content = turn_planner.build_turn_plan_message(
        TurnPlan(requires_check=True, check=CheckPlan(skill="幸运", chars="在场"))
    )["content"]
    assert "[DICE_CHECK: skill=幸运, difficulty=normal, chars=在场]" in content
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


def test_build_turn_plan_message_开战时注入状态硬约束():
    from app.ai.turn_planner import CombatPlan, TurnPlan

    plan = TurnPlan(
        turn_kind="combat",
        player_intent="攻击循声者",
        combat=CombatPlan(
            should_start=True,
            enemies=["循声者"],
            trigger="调查员冲向循声者发动攻击",
        ),
    )
    content = turn_planner.build_turn_plan_message(plan)["content"]
    assert "结构化战斗切换" in content
    assert "必须调用 start_combat" in content
    assert "循声者" in content
    assert "确定性补偿" in content


def test_turn_plan_开战时取消普通检定():
    from app.ai.turn_planner import CheckPlan, CombatPlan, TurnPlan

    plan = TurnPlan(
        turn_kind="combat",
        requires_check=True,
        check=CheckPlan(skill="格斗(斗殴)"),
        combat=CombatPlan(should_start=True, enemies=["循声者"]),
    )
    assert plan.combat.should_start is True
    assert plan.requires_check is False


def test_auto_outcome_与检定互斥且非法值归一():
    """自动结局（success/failure）与掷骰互斥：置了自动结局就强制 requires_check=false；
    非法值归一为 none；开战优先，取消自动结局。"""
    from app.ai.turn_planner import CombatPlan, TurnPlan

    p = TurnPlan(requires_check=True, auto_outcome="failure")
    assert p.auto_outcome == "failure" and p.requires_check is False
    assert TurnPlan(auto_outcome="乱写").auto_outcome == "none"          # 非法→none
    # 模型常写 auto_outcome: null —— 必须容错为 none，绝不能让整份计划校验失败回退旧流程
    assert TurnPlan.model_validate({"auto_outcome": None}).auto_outcome == "none"
    assert TurnPlan.model_validate({"auto_outcome_reason": None}).auto_outcome_reason == ""
    p3 = TurnPlan(auto_outcome="success", combat=CombatPlan(should_start=True, enemies=["怪"]))
    assert p3.auto_outcome == "none"                                     # 开战取消自动结局


def test_标量str字段容忍dict_list_保住整份计划():
    """模型把自由文本标量字段写成 dict/list（如 player_intent={'actor':…,'intent':…}）时，
    必须就地转字符串、保住整份 TurnPlan，绝不能因一个字段撞 str 类型被整体丢弃回退旧流程。
    这是线上真实报错（player_intent 收到 dict）的回归。"""
    from app.ai.turn_planner import TurnPlan

    # 顶层 player_intent 写成 dict → 拼各值成句，内容不丢
    p = TurnPlan.model_validate({
        "player_intent": {"actor": "江户川龙牙", "intent": "驾驶或控制车体"},
        "requires_check": True,
    })
    assert isinstance(p.player_intent, str)
    assert "江户川龙牙" in p.player_intent and "驾驶或控制车体" in p.player_intent
    assert p.requires_check is True                    # 整份计划保住，其它字段照常

    # 顶层 auto_outcome_reason 写成 list → 拼接
    p2 = TurnPlan.model_validate({"auto_outcome_reason": ["已暴露", "仍想潜行"]})
    assert p2.auto_outcome_reason == "已暴露；仍想潜行"

    # 子模型内部标量 str（check.reason）写成 dict → 就地容错，不连累整份计划
    p3 = TurnPlan.model_validate({"check": {"skill": "聆听", "reason": {"why": "隔墙有声"}}})
    assert p3.check.skill == "聆听" and "隔墙有声" in p3.check.reason


def test_sanity_loss_容忍整数与null():
    """SAN 损失字段是「骰式/数字」，模型常写成 int 0/1 或 null —— 必须 str 化容错，
    否则整份计划因 str 类型校验失败回退旧流程（丢掉全部裁定信号，评测里已复现）。"""
    from app.ai.turn_planner import TurnPlan

    p = TurnPlan.model_validate({"sanity": {"trigger": True, "success_loss": 0, "failure_loss": 1}})
    assert p.sanity.success_loss == "0" and p.sanity.failure_loss == "1"
    p2 = TurnPlan.model_validate({"sanity": {"success_loss": None, "failure_loss": None}})
    assert p2.sanity.success_loss == "0" and p2.sanity.failure_loss == "1d6"   # None→字段默认


def test_build_turn_plan_message_注入自动结局硬约束():
    """auto_outcome=failure 时注入「直接失败、据此确定性叙述、绝不写成侥幸成功」的硬约束（含入戏缘由）。"""
    from app.ai.turn_planner import TurnPlan

    plan = TurnPlan(auto_outcome="failure",
                    auto_outcome_reason="手机巨响已把循声者引到玩家位置，行踪彻底暴露")
    content = turn_planner.build_turn_plan_message(plan)["content"]
    assert "自动结局" in content and "直接失败" in content
    assert "循声者" in content                                            # 入戏缘由被带上
    assert "绝不能" in content                                            # 明确禁止写成侥幸成功
    # success 走另一支：兑现为实打实进展
    ok = turn_planner.build_turn_plan_message(
        TurnPlan(auto_outcome="success", auto_outcome_reason="话术切中动机且承诺保密"))["content"]
    assert "直接成功" in ok
    # none（默认）不注入
    assert "自动结局" not in turn_planner.build_turn_plan_message(TurnPlan())["content"]


def test_turn_plan_prompt_含裁定准则与原型例():
    """规划器提示必须给出「虚构态势→难度/奖惩骰/免检」的裁定准则与两条原型例，
    否则 auto_outcome / bonus / penalty 只是无人会用的死字段。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="亨利", rule_system="coc", is_player=True, system_data={})
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active",
                    world_state={}, current_scene_id=None)
    db.add(s); db.commit()
    ev = EventLog(session_id=s.id, sequence_num=1, event_type="action",
                  actor_id=pc.id, actor_name="亨利", content="我想潜行")
    db.add(ev); db.commit()
    msgs = turn_planner.build_turn_plan_messages(s, module, pc, [ev])
    text = "".join(m["content"] for m in msgs)
    assert "裁定准则" in text and "auto_outcome" in text
    assert "循声" in text and "话术" in text          # 两条原型例都在
    assert "别让口才碾平一切" in text                  # 高风险防滥用护栏


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
    """空/纯文本/无大括号 → 无法解析 JSON → 回退 None，不阻塞跑团。"""
    for junk in ["", "抱歉，我无法生成计划", "no braces here"]:
        assert await turn_planner.run_turn_planner(_RawLLM(junk), []) is None


@pytest.mark.asyncio
async def test_run_turn_planner_tolerates_bad_field_shapes():
    """合法 JSON 但个别字段形状/枚举写错 → 归一而非丢弃整份计划（否则次要字段拖垮核心裁定）。"""
    plan = await turn_planner.run_turn_planner(
        _RawLLM('{"turn_kind":"invalid_kind","safety":"安全，无威胁"}'), [],
    )
    assert plan is not None
    assert plan.turn_kind == "mixed"  # 非法枚举退默认
    assert plan.safety.do_not_reveal == []  # 句子形状的 safety 退默认


def test_turn_plan_messages_include_truth_and_scene_events(db_factory):
    """payload 带模组幕后真相与当前场景机制点（events）；指令要求命中时数值照抄、不得估值。"""
    db = db_factory()
    module, hero, session = _seed(db)
    module.truth = "真相：管家杀害了主人并伪装成意外。"
    scenes = [dict(s) for s in module.scenes]
    for s in scenes:
        if s.get("id") == "study":
            s["events"] = [{"trigger": "翻动书桌后的尸体", "kind": "san_check", "san_loss": "0/1d3"}]
    module.scenes = scenes
    db.commit()

    messages = turn_planner.build_turn_plan_messages(
        session, module, hero, [], teammates=[], rules_lookup_enabled=False,
    )
    text = "\n".join(m["content"] for m in messages)
    assert "管家杀害了主人" in text                    # truth 进 payload
    assert "0/1d3" in text and "翻动书桌后的尸体" in text  # 当前场景 events 进 payload
    instruction = messages[1]["content"]
    assert "机制点" in instruction and "照抄" in instruction
