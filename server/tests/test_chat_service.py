"""chat_service 流式持久化与 opening 幂等的回归测试。

用临时文件 SQLite + monkeypatch 模拟断流，不依赖真实 LLM。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401 — 注册建表
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_session(db) -> str:
    module = Module(title="测试模组", rule_system="coc", npcs=[])
    char = Character(name="测试角色", rule_system="coc")
    db.add(module)
    db.add(char)
    db.flush()
    session = GameSession(
        module_id=module.id,
        player_character_id=char.id,
        status="active",
    )
    db.add(session)
    db.commit()
    return session.id


async def _collect(agen) -> list:
    return [chunk async for chunk in agen]


def _narrations(db_factory, session_id) -> list:
    return [
        e
        for e in session_service.get_session_events(db_factory(), session_id)
        if e.event_type == "narration"
    ]


class _FakeKP:
    def __init__(self, text):
        self.text = text

    async def narrate(self, messages):
        for ch in self.text:
            yield ch


def test_player_adjacent_quote_not_extracted():
    """玩家方角色附近的引号文本（KP 误代言/书写内容）不应被抽成对话气泡，
    也不应被错误地归给附近的模组 NPC——整段留在旁白里。"""
    text = (
        "失踪的萨沙·卡纳的帐篷扎在西面。"
        "约翰·卡特似乎想到了这一点，他开口道：“我们先去帐篷找线索吧。”"
    )
    npcs = [{"name": "萨沙·卡纳"}, {"name": "约翰·卡特", "is_player": True}]
    result = ["", "", []]
    chunks = asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert speakers == []  # 玩家方引号不抽取
    assert "萨沙·卡纳" not in speakers  # 也不会被错记到 NPC 头上
    assert not any('"npc_dialogue"' in c for c in chunks)


def test_narrative_colon_not_treated_as_speaker():
    """以冒号收尾的叙述句（「他指了指墙上的四个门：」）不能把名词短语当成说话人；
    台词应归给最近行动的 NPC（诺特），而非「墙上的四个门」。"""
    text = (
        "诺特站起身，从口袋里掏出一把小钥匙。"
        "他指了指墙上的四个门：“你们说去图书馆或档案馆——都可以。”"
    )
    npcs = [{"name": "史蒂芬·诺特"}]
    result = ["", "", []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert "墙上的四个门" not in speakers  # 名词短语不会被当成名字
    assert speakers == ["史蒂芬·诺特"]     # 归给最近行动的 NPC


def test_dialogue_after_paragraph_break_still_attributed():
    """台词另起一段时，前文主语（诺特）已被 flush 进 narration；说话人解析需基于
    narration+pending，否则会漏判说话人、台词被错留在旁白。"""
    text = (
        "诺特听完了你们的讨论，微微颔首，似乎对你们的谨慎态度表示赞许。"
        "他站起身，从口袋里掏出一把小钥匙，走到房间北墙边，打开柜子，"
        "翻了翻，找出几张纸，走回来递给亨利和约翰。\n\n"
        "“这是我从遗产律师那儿拿到的一些零散文件，里面有科比特当年的遗嘱摘要。”"
    )
    npcs = [{"name": "史蒂芬·诺特"}]
    result = ["", "", []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert speakers == ["史蒂芬·诺特"]  # 跨段落仍能归到诺特


def test_prefix_speaker_label_not_duplicated_in_narration():
    """「史蒂芬·诺特：「台词」」抽成气泡后，前缀「史蒂芬·诺特：」不应再留在旁白里重复显示。"""
    text = "他顿了顿。史蒂芬·诺特：“他们在面包店打工，从不惹事。”"
    npcs = [{"name": "史蒂芬·诺特"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert speakers == ["史蒂芬·诺特"]          # 台词归到诺特
    assert "史蒂芬·诺特：" not in result[0]      # 前缀已从旁白抹掉，不重复
    assert "他顿了顿。" in result[0]


def test_long_speaker_name_prefix_fully_stripped():
    """长说话人名（超 6 字，如「加布里埃尔·马卡里奥：」）抽成气泡后，前缀应整体抹掉，
    不残留半截名字（「加布里埃」）在旁白里。"""
    text = (
        "她的声音压得极低，仿佛害怕被什么东西听见。\n\n"
        "加布里埃尔·马卡里奥：“不是……不是鬼魂。它古老。”"
    )
    npcs = [{"name": "加布里埃尔·马卡里奥"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert [n for n, _ in result[2]] == ["加布里埃尔·马卡里奥"]   # 全名归属
    assert "加布里埃" not in result[0]                          # 无半截名残留


def test_last_speaker_released_after_paragraph_break():
    """上一位说话人不应跨段把后文（如另一场景读到的报纸短讯）吸成自己的台词。"""
    text = (
        "史蒂芬·诺特压低声音：“她说，特蕾莎在跟不存在的人说话。”\n\n"
        "亨利翻开那叠剪报，《波士顿环球报》上的一则短讯：“北区老宅传响，住户深夜报警”，"
        "配了一张模糊的照片。"
    )
    npcs = [{"name": "史蒂芬·诺特"}, {"name": "亨利·卡特", "is_player": True}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert speakers == ["史蒂芬·诺特"]               # 只有诺特那句是台词
    assert "北区老宅传响" not in " ".join(t for _, t in result[2])  # 报纸短讯不被抽成台词
    assert "北区老宅传响" in result[0]               # 它留在旁白


def test_quoted_label_list_all_stay_in_narration():
    """一串书写标识引号（门牌列表）：首个被「铭牌：」判为书写后，相邻的同串引号一律
    留旁白——即便末项较长、附近又有 NPC 名（诺特），也不会被误抽成台词。"""
    text = (
        "诺特先生清了清嗓子，声音里带着一丝焦虑：他停顿了一下。\n\n"
        "他把信封放在书桌上。房间北墙有四扇门，门上的铭牌："
        "“波士顿环球报社”“中央图书馆”“市立档案馆”“科比特老宅——14号麦瑟街”。"
    )
    npcs = [{"name": "史蒂芬·诺特"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == []  # 门牌列表无一被抽成台词
    for label in ("波士顿环球报社", "中央图书馆", "市立档案馆", "科比特老宅——14号麦瑟街"):
        assert label in result[0]


def test_written_text_near_player_stays_in_narration():
    """书写/刻字内容（如笔记封面字母）即便用引号包裹，靠近玩家角色名时也留在旁白。"""
    text = (
        "詹姆斯·卡特弯下腰，从碎石堆里拾起那本手记。"
        "封面正中用烫金压着一行字母——“S. KANA · THEBES · 1915”。"
    )
    npcs = [{"name": "詹姆斯·卡特", "is_player": True}]
    result = ["", "", []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == []  # 没有对话被抽取
    assert "S. KANA" in result[0]  # 字母留在旁白文本里


def test_fullwidth_bracket_command_tag_not_leaked():
    """模型用全角括号/漏冒号写指令（【SET_FLAG hint_x】）也应被当指令剔除，不泄漏进旁白。"""
    text = "她一字一顿地说完那句话。\n\n【SET_FLAG hint_leviticus_25_10】"
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result)
    ))
    assert "SET_FLAG" not in result[0] and "hint_leviticus" not in result[0]
    assert result[0].strip() == "她一字一顿地说完那句话。"


def test_blind_roll_result_not_leaked_to_narration():
    """暗投/暗骰的裁定结果（仅 KP 可见）若被模型误写进方括号，绝不能回吐进旁白。
    覆盖：半角/全角括号、结尾成败、暗骰 NPC 结果。"""
    text = (
        "你注视着加布里埃尔的双眼，试图捕捉更深的情绪。"
        "[暗投结束 - 伊芙琳·哈特·心理学检定 失败]"
        "她整个人像被掏空后又勉强拼凑起来。\n\n"
        "护士站在门边。【暗骰·护士·潜行 成功】她的脚步没有发出声响。"
    )
    npcs = [{"name": "加布里埃尔·马卡里奥"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    # 泄漏标记整体被丢弃：既无「暗投/暗骰」字样，也无「检定 失败/潜行 成功」成败
    assert "暗投" not in result[0] and "暗骰" not in result[0]
    assert "心理学检定" not in result[0] and "失败" not in result[0]
    assert "潜行" not in result[0] and "成功" not in result[0]
    # 正常叙述文字保留、不被误删
    assert "试图捕捉更深的情绪" in result[0]
    assert "勉强拼凑起来" in result[0]
    assert "脚步没有发出声响" in result[0]


def test_set_flag_regex_tolerant():
    """SET_FLAG 正则容忍漏写 flag=／冒号写成空格（全角括号在 _process_commands 里已归一）。"""
    assert chat_service.SET_FLAG_RE.findall("[SET_FLAG: flag=basement_flooded]") == ["basement_flooded"]
    assert chat_service.SET_FLAG_RE.findall("[SET_FLAG hint_x]") == ["hint_x"]
    assert chat_service.SET_FLAG_RE.findall("[SET_FLAG:door_open]") == ["door_open"]


def test_resolve_scene_ref_id_or_name():
    """SCENE_CHANGE 的引用按 id 或场景名稳健解析；解析不到返回 None（不乱改当前场景）。"""
    mod = Module(title="t", rule_system="coc", scenes=[
        {"id": "scene_1", "name": "诺特的办公室"},
        {"id": "scene_2", "name": "圣玛丽疗养院"},
    ])
    r = chat_service._resolve_scene_ref
    assert r(mod, "scene_2") == "scene_2"        # 精确 id
    assert r(mod, "圣玛丽疗养院") == "scene_2"     # 精确名
    assert r(mod, "疗养院") == "scene_2"           # 名字互含（KP 写简称）
    assert r(mod, "不存在的地方") is None          # 解析不到 → None
    assert chat_service._scene_name(mod, "scene_1") == "诺特的办公室"


def test_say_marker_extracts_dialogue():
    """显式 [SAY] 标记把 NPC 台词抽成对话（局部名归一到全名）。"""
    text = "托马斯·金博尔露出和蔼的微笑。[SAY: who=托马斯]下午好，年轻人。[/SAY]他望向门口。"
    npcs = [{"name": "托马斯·金博尔"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == [("托马斯·金博尔", "下午好，年轻人。")], result[2]
    assert "[SAY" not in result[0] and "[/SAY]" not in result[0]  # 标记不入旁白
    assert "望向门口" in result[0]


def test_unnamed_npc_via_say():
    """无名 NPC（护工）用其身份作 who，归到该身份，不混入有名 NPC。"""
    npcs = [{"name": "史蒂芬·诺特"}]
    text = (
        "[SAY: who=史蒂芬·诺特]你们去疗养院吧。[/SAY]\n\n"
        "一位路过的护工迎上来。[SAY: who=护工]您是来找海恩斯院长的吧？[/SAY]"
    )
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert [n for n, _ in result[2]] == ["史蒂芬·诺特", "护工"], result[2]


def test_bare_quotes_never_extracted():
    """普通引号（被提及的词/门牌标识/书写内容）一律留旁白，绝不抽成对话气泡。"""
    npcs = [{"name": "维托里奥·马卡里奥"}, {"name": "史蒂芬·诺特"}]
    text = (
        "前租户——马卡里奥一家——卷入悲剧。诺特先生听到“考古发现”这个词时皱了皱眉。\n\n"
        "门上贴着褪色的字牌——“恢复名誉”、“波士顿环球报社”。照片角落写着“1899年”。"
    )
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == [], result[2]            # 无任何引号被错抽成对话
    for kw in ("考古发现", "恢复名誉", "1899年"):
        assert kw in result[0]                   # 都留在旁白


def test_group_tags_split_scenes(db_factory):
    """分头行动：[GROUP] 标记把各组内容落库到对应 group，供前端分栏。"""
    npcs = [{"name": "护工"}]
    text = (
        "[GROUP: scene=档案馆]亨利在档案馆翻查旧卷宗，灰尘扑面。\n\n"
        "[GROUP: scene=疗养院]莫妮卡走进疗养院走廊。[SAY: who=护工]您找海恩斯院长？[/SAY]"
    )
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    db = db_factory()
    session_id = _seed_session(db)
    chat_service._persist_narration(db, session_id, result)
    import json as _json
    evs = session_service.get_session_events(db_factory(), session_id)
    groups = [(_json.loads(e.metadata_ or "{}") if isinstance(e.metadata_, str) else (e.metadata_ or {})).get("group") for e in evs]
    # 两组内容各自带 group；标记本身不出现在旁白文本里
    assert "档案馆" in groups and "疗养院" in groups
    assert all("[GROUP" not in (e.content or "") for e in evs)


def test_group_label_tags_all_output(db_factory):
    """分头行动按组生成：group_label 给定时，整段产物（流式 + 落库）确定性归入该组，
    不依赖模型自觉打 [GROUP]。"""
    import json as _json
    npcs = [{"name": "管理员"}]
    text = "约翰推开图书馆厚重的门，翻查旧报。[SAY: who=管理员]这边请。[/SAY]"
    result = ["", "", [], [], []]
    chunks = asyncio.run(_collect(
        chat_service._stream_narration_filtered(
            _FakeKP(text), [], result, npcs=npcs, group_label="图书馆",
        )
    ))
    payloads = [_json.loads(c[len("data: "):]) for c in chunks]
    assert payloads and all(
        p.get("metadata", {}).get("group") == "图书馆"
        for p in payloads if p["type"] in ("narration", "npc_dialogue")
    )
    db = db_factory()
    session_id = _seed_session(db)
    chat_service._persist_narration(db, session_id, result)
    evs = session_service.get_session_events(db_factory(), session_id)
    groups = [
        (_json.loads(e.metadata_) if isinstance(e.metadata_, str) else (e.metadata_ or {})).get("group")
        for e in evs
    ]
    assert groups and all(g == "图书馆" for g in groups)


def test_split_focus_prompt_forbids_acting_for_members():
    """分头聚焦提示词必须禁止 KP 替该场景成员（玩家角色）说话/行动，只叙述场景与 NPC 反应。"""
    p = chat_service.SPLIT_FOCUS_PROMPT.format(label="疗养院", members="莫妮卡")
    assert "绝不替" in p and ("说话" in p and "行动" in p)
    assert "玩家角色" in p


def test_location_groups_by_actual_scene(db_factory):
    """按每人真实所在场景归并：全队同处不分头；有人 travel 到别处则分头，同场景合一列。"""
    db = db_factory()
    module = Module(
        title="M", rule_system="coc", npcs=[],
        scenes=[{"id": "scene_office", "title": "事务所"},
                {"id": "scene_lib", "title": "中央图书馆"}],
    )
    pc = Character(name="莫妮卡·卡佩尔", rule_system="coc")
    t1 = Character(name="亨利·卡特", rule_system="coc")
    t2 = Character(name="约翰·卡特", rule_system="coc")
    db.add_all([module, pc, t1, t2]); db.flush()
    sess = GameSession(module_id=module.id, player_character_id=pc.id, status="active",
                       current_scene_id="scene_office", world_state={})
    db.add(sess); db.commit()

    # 全队默认同处「事务所」→ 不分头（1 组）
    g0 = chat_service._location_groups(sess, module, pc, [t1, t2])
    assert len(g0) == 1 and g0[0]["label"] == "事务所"

    # 亨利 travel 到图书馆 → 2 个场景，分头；约翰仍与玩家同处
    session_service.set_char_location(db, sess.id, t1.id, "scene_lib")
    sess = db.get(GameSession, sess.id)
    g1 = chat_service._location_groups(sess, module, pc, [t1, t2])
    by = {x["label"]: set(x["members"]) for x in g1}
    assert by == {"事务所": {"莫妮卡·卡佩尔", "约翰·卡特"}, "中央图书馆": {"亨利·卡特"}}
    assert g1[0]["label"] == "事务所"  # 玩家所在列在前


def test_tag_turn_events_by_group(db_factory):
    """本回合各角色的行动/对话/掷骰按其所在分组补打 group 标签（玩家行动随场景列同列）。"""
    db = db_factory()
    session_id = _seed_session(db)
    e_player = session_service.add_event(db, session_id, "action", "我走进疗养院", actor_name="莫妮卡·卡佩尔")
    e_henry = session_service.add_event(db, session_id, "dialogue", "亨利问管理员", actor_name="亨利·卡特")
    e_dice = session_service.add_event(db, session_id, "dice", "亨利·卡特｜图书馆使用 检定：失败", actor_name="系统")
    groups = [
        {"label": "疗养院", "members": ["莫妮卡·卡佩尔"]},
        {"label": "档案馆", "members": ["亨利·卡特"]},
    ]
    chat_service._tag_turn_events_by_group(db, [e_player, e_henry, e_dice], groups)
    by_id = {e.id: (e.metadata_ or {}).get("group")
             for e in session_service.get_session_events(db_factory(), session_id)}
    assert by_id[e_player.id] == "疗养院"
    assert by_id[e_henry.id] == "档案馆"
    assert by_id[e_dice.id] == "档案馆"  # 掷骰按内容领头角色名归组


def test_written_card_text_with_markdown_not_extracted():
    """书写内容前夹着 markdown 标记（写着：> **「…」**）时仍判为书写、不抽成台词。"""
    text = "亨利抽出一张卡片，上面写着：> **「特里蒙特地产信托公司——成立于1903年」**"
    npcs = [{"name": "沃尔特·科比特"}]
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == []  # 卡片书写内容不被当成对话


def test_parse_team_decision_accepts_travel():
    """队友决策解析：新增 travel 动作与 target 字段。"""
    d = chat_service._parse_team_decision(
        '{"action":"travel","content":"我去图书馆查查","target":"中央图书馆"}')
    assert d and d["action"] == "travel" and d["target"] == "中央图书馆"
    # 普通动作仍照常
    d2 = chat_service._parse_team_decision('{"action":"speak","content":"我同意"}')
    assert d2["action"] == "speak" and d2["target"] == ""
    # 未知动作仍被拒
    assert chat_service._parse_team_decision('{"action":"fly","content":"x"}') is None


def test_skill_names_from_dict_and_system_data():
    c1 = Character(name="a", rule_system="coc", skills={"心理学": 65})
    assert "心理学" in chat_service._skill_names(c1)
    c2 = Character(name="b", rule_system="coc", system_data={"skills": {"侦查": 60}})
    assert "侦查" in chat_service._skill_names(c2)


def test_detect_check_request_routes_only_real_requests():
    """意图分诊：玩家主动申请检定 → 返回技能名；普通行动 → None（走常规叙事）。"""
    class _LLM:
        def __init__(self, resp):
            self.resp = resp

        async def complete(self, messages, temperature=0.7, **kw):
            return self.resp

    char = Character(name="莫妮卡", rule_system="coc", skills={"心理学": 65, "侦查": 60})
    got = asyncio.run(chat_service._detect_check_request(
        _LLM('{"check": true, "skill": "心理学"}'), "我用心理学看看他说的是真是假", char))
    assert got == "心理学"
    none = asyncio.run(chat_service._detect_check_request(
        _LLM('{"check": false}'), "我走进房间四处看看", char))
    assert none is None


def test_check_request_generation_includes_intent(db_factory, monkeypatch):
    """/check 申请检定时，玩家顺带说明的目标要真正进到 KP 的裁定提示词里——否则场景里同时
    有多条线索/多个可疑点时，KP 光看技能名猜不出玩家的具体目标。"""
    _patch_runtime(monkeypatch, db_factory)
    captured = {}

    async def fake_run_kp_turn(db, session_id, game_session, module, player_char, party_others, user_prompt):
        captured["prompt"] = user_prompt

    monkeypatch.setattr(chat_service, "_run_kp_turn", fake_run_kp_turn)

    db = db_factory()
    session_id = _seed_session(db)
    actor_id = db.get(GameSession, session_id).player_character_id

    asyncio.run(chat_service.run_check_request_generation(session_id, actor_id, "侦查", "搜查书桌暗格"))

    assert "搜查书桌暗格" in captured["prompt"]
    assert "侦查" in captured["prompt"]


def test_check_request_generation_without_intent_prompts_kp_to_infer(db_factory, monkeypatch):
    """不带 intent 时（旧客户端未传/未填写），提示词要明确要求 KP 自行结合情境判断，而非留空。"""
    _patch_runtime(monkeypatch, db_factory)
    captured = {}

    async def fake_run_kp_turn(db, session_id, game_session, module, player_char, party_others, user_prompt):
        captured["prompt"] = user_prompt

    monkeypatch.setattr(chat_service, "_run_kp_turn", fake_run_kp_turn)

    db = db_factory()
    session_id = _seed_session(db)
    actor_id = db.get(GameSession, session_id).player_character_id

    asyncio.run(chat_service.run_check_request_generation(session_id, actor_id, "侦查"))

    assert "结合当前情境自行判断意图" in captured["prompt"]


def test_generation_check_intent_detection_forwards_player_text(db_factory, monkeypatch):
    """自由文本触发的检定申请（如「我用侦查看看书桌暗格」）同样要把这句话带进裁定提示词，
    这条路径此前和 /check 端点一样漏了这一环。"""
    _patch_runtime(monkeypatch, db_factory)
    captured = {}

    async def fake_detect(llm, text, char):
        return "侦查"

    async def fake_run_kp_turn(db, session_id, game_session, module, player_char, party_others, user_prompt):
        captured["prompt"] = user_prompt

    monkeypatch.setattr(chat_service, "_detect_check_request", fake_detect)
    monkeypatch.setattr(chat_service, "_run_kp_turn", fake_run_kp_turn)

    db = db_factory()
    session_id = _seed_session(db)
    actor_id = db.get(GameSession, session_id).player_character_id
    session_service.add_event(
        db, session_id, "action", "我用侦查看看书桌暗格",
        actor_id=actor_id, actor_name="测试角色",
    )

    asyncio.run(chat_service.run_chat_generation(session_id))

    assert "书桌暗格" in captured["prompt"]


def test_persisted_order_interleaves_narration_and_dialogue(db_factory):
    """落库要保留「旁白/对话交错」的原始顺序（与流式渲染一致），而非旁白全在前、对话全在后。"""
    npcs = [{"name": "史蒂芬·诺特"}]
    text = (
        "诺特先生走上前来。[SAY: who=史蒂芬·诺特]万幸你们来了。[/SAY]"
        "他压低声音，凑近了些。[SAY: who=史蒂芬·诺特]房子就在彻斯特街。[/SAY]"
        "他递来一把铜钥匙。"
    )
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))

    db = db_factory()
    session_id = _seed_session(db)
    chat_service._persist_narration(db, session_id, result)

    evs = session_service.get_session_events(db_factory(), session_id)
    kinds = [e.event_type for e in evs]
    # 交错：旁白 → 对话 → 旁白 → 对话 → 旁白（而非 narration*1 + dialogue*2）
    assert kinds == ["narration", "dialogue", "narration", "dialogue", "narration"]
    assert [e.actor_name for e in evs if e.event_type == "dialogue"] == ["史蒂芬·诺特", "史蒂芬·诺特"]


def test_split_speech_action_quote_convention():
    """引号约定：引号内=台词(dialogue)，引号外=行动(action)，保序；无引号整条按行动。"""
    f = chat_service.split_speech_action
    assert f("我走近向导，“附近有水源吗？”") == [
        ("action", "我走近向导"), ("dialogue", "附近有水源吗？"),
    ]
    assert f("「你好，金博尔先生」") == [("dialogue", "你好，金博尔先生")]
    assert f("我仔细搜索房间") == [("action", "我仔细搜索房间")]
    assert f('"Hello" 然后我后退一步') == [
        ("dialogue", "Hello"), ("action", "然后我后退一步"),
    ]
    assert f("   ") == []


def test_multiparagraph_narration_with_npcs_does_not_crash():
    """旁白含段落分隔 \\n\\n 且模组有 NPC 时，段落缓冲分支会遍历 npc_matchers（3 元组）。
    曾因该分支用 2 元组解包导致 ValueError，让整段生成崩溃、前端收不到开场白。"""
    text = "第一段旁白内容。\n\n第二段旁白内容，继续描述场景。\n\n第三段，收束。"
    npcs = [{"name": "老向导"}]
    result = ["", "", []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert "第一段旁白内容" in result[0]
    assert "第三段" in result[0]


def test_module_intro_card_from_metadata(db_factory):
    """背景导语卡：取模组公开元信息（类型/年代/地区/难度/人数）+ 一句话前提，落成 system 事件。"""
    db = db_factory()
    module = Module(
        title="常暗之箱", rule_system="coc", npcs=[],
        description="在末班电车上醒来，发现身处诡异空间，必须寻找出路。",
        world_setting={"tone": "恐怖、悬疑", "era": "现代", "region": "日本",
                       "difficulty": "普通", "player_count": "2-3"},
    )
    char = Character(name="测试角色", rule_system="coc")
    db.add(module); db.add(char); db.flush()
    session = GameSession(module_id=module.id, player_character_id=char.id, status="active")
    db.add(session); db.commit()

    chunk = chat_service._persist_module_intro(db, session.id, module)
    assert chunk is not None
    evs = session_service.get_session_events(db_factory(), session.id)
    intro = [e for e in evs if (e.metadata_ or {}).get("kind") == "module_intro"]
    assert len(intro) == 1
    md = intro[0].metadata_
    assert md["title"] == "常暗之箱"
    assert md["meta"] == "恐怖、悬疑 · 现代 · 日本 · 难度 普通 · 建议 2-3 人"
    assert "末班电车" in intro[0].content


def _patch_runtime(monkeypatch, db_factory):
    """把 chat_service 的运行期依赖换成测试可控的桩。"""
    import app.database as database
    from app.services.room_hub import room_hub

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_service, "get_llm", lambda: None)
    monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)


def test_generation_saves_on_interrupt(db_factory, monkeypatch):
    """流式被取消（硬取消生成 task）时，已生成内容仍应落库。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "KP 刚说到一半"
        yield chat_service._make_chunk("narration", "KP 刚说到一半", actor_name="KP")
        raise asyncio.CancelledError()

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "dialogue", "我环顾四周", actor_name="玩家")

    # run_chat_generation 内部吞掉 CancelledError，但叙事应已在 finally 落库
    asyncio.run(chat_service.run_chat_generation(session_id))

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "KP 刚说到一半"


def test_opening_saves_partial_on_stream_error(db_factory, monkeypatch):
    """开场流式中途报错（供应商抖动断流）时，已生成片段应落库，避免客户端 resync 后聊天清空。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "雾气弥漫的码头，远处传来"
        yield chat_service._make_chunk("narration", "雾气弥漫的码头，远处传来", actor_name="KP")
        raise RuntimeError("provider stream dropped")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    # 不应抛出（run_opening_generation 吞异常并落库提示）
    asyncio.run(chat_service.run_opening_generation(session_id))

    # 已生成的开场片段落库（非空，绝不丢成空历史）
    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "雾气弥漫的码头，远处传来"
    # 同时落了一条系统提示，供 resync 后仍可见
    systems = [e for e in session_service.get_session_events(db_factory(), session_id) if e.event_type == "system"]
    assert any("中断" in (e.content or "") for e in systems)


def test_generation_saves_once_on_success(db_factory, monkeypatch):
    """正常完成时落库一次且不重复。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "完整的开场叙事"
        yield chat_service._make_chunk("narration", "完整的开场叙事", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    asyncio.run(chat_service.run_opening_generation(session_id))

    narrations = _narrations(db_factory, session_id)
    assert len(narrations) == 1
    assert narrations[0].content == "完整的开场叙事"


def test_generation_injects_turn_plan(db_factory, monkeypatch):
    """常规 KP 生成前应先运行回合规划器，并把裁定计划注入 KP 上下文。"""
    _patch_runtime(monkeypatch, db_factory)
    captured = {}

    async def fake_run_turn_planner(llm, messages):
        return chat_service.turn_planner.TurnPlan(
            turn_kind="investigate",
            player_intent="搜查书桌",
            requires_check=True,
            check=chat_service.turn_planner.CheckPlan(skill="侦查"),
            safety=chat_service.turn_planner.SafetyPolicy(do_not_reveal=["管家的秘密"]),
        )

    async def fake_stream(kp, messages, result, npcs=None):
        captured["messages"] = messages
        result[0] = "你开始检查书桌。"
        yield chat_service._make_chunk("narration", "你开始检查书桌。", actor_name="KP")

    async def fake_process(*args, **kwargs):
        if False:
            yield None

    monkeypatch.setattr(chat_service.turn_planner, "run_turn_planner", fake_run_turn_planner)
    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)
    monkeypatch.setattr(chat_service, "_process_commands", fake_process)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "action", "我搜查书桌", actor_name="测试角色")

    asyncio.run(chat_service.run_chat_generation(session_id))

    text = "\n".join(message["content"] for message in captured["messages"])
    assert "【本轮裁定计划】" in text
    assert "搜查书桌" in text
    assert "管家的秘密" in text


def test_generation_patches_narration_when_validator_flags_violation(db_factory, monkeypatch):
    """回合校验器（阶段 2）判定违规时，落库版本应换成改写文本，不把汇报体/内部 flag id
    永久留在会话记录里；对话仍要保留（交错偏移已失真，改走「整段旁白+对话追加」回退路径）。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_run_turn_planner(llm, messages):
        return chat_service.turn_planner.TurnPlan(
            safety=chat_service.turn_planner.SafetyPolicy(do_not_reveal=["管家的秘密"]),
        )

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "【场景状态更新】\n- flag hint_x 仍需调查员获取管家的秘密。"
        result[1] = result[0]
        result[2] = [("管家", "别问我。")]
        result[3] = [(0, "管家", "别问我。")]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    async def fake_validate(llm, plan, narration):
        return chat_service.turn_validator.TurnValidation(
            violated=True, reason="汇报体+泄露", corrected_narration="房间陷入了短暂的沉默。",
        )

    async def fake_process(*args, **kwargs):
        if False:
            yield None

    monkeypatch.setattr(chat_service.turn_planner, "run_turn_planner", fake_run_turn_planner)
    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)
    monkeypatch.setattr(chat_service.turn_validator, "validate_turn_narration", fake_validate)
    monkeypatch.setattr(chat_service, "_process_commands", fake_process)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "action", "我搜查书桌", actor_name="测试角色")

    asyncio.run(chat_service.run_chat_generation(session_id))

    evs = session_service.get_session_events(db_factory(), session_id)
    narrations = [e.content for e in evs if e.event_type == "narration"]
    dialogues = [e.content for e in evs if e.event_type == "dialogue"]
    assert narrations == ["房间陷入了短暂的沉默。"]
    assert "flag hint_x" not in "".join(narrations)
    assert dialogues == ["别问我。"]  # 对话仍保留，不因改写而丢失


def test_split_generation_injects_turn_plan_into_each_group(db_factory, monkeypatch):
    """分头行动（队伍身处不同场景）不应退化回纯提示词——每个分组也要收到本轮裁定计划。"""
    _patch_runtime(monkeypatch, db_factory)
    captured = []

    async def fake_run_turn_planner(llm, messages):
        return chat_service.turn_planner.TurnPlan(
            turn_kind="mixed",
            player_intent="分头行动",
            safety=chat_service.turn_planner.SafetyPolicy(do_not_reveal=["管家的秘密"]),
        )

    async def fake_stream(kp, messages, result, npcs=None, group_label=None):
        captured.append((group_label, messages))
        result[0] = f"{group_label} 的叙事"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    async def fake_process(*args, **kwargs):
        if False:
            yield None

    monkeypatch.setattr(chat_service.turn_planner, "run_turn_planner", fake_run_turn_planner)
    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)
    monkeypatch.setattr(chat_service, "_process_commands", fake_process)

    db = db_factory()
    module = Module(
        title="M", rule_system="coc", npcs=[],
        scenes=[{"id": "scene_office", "name": "事务所"}, {"id": "scene_lib", "name": "图书馆"}],
    )
    pc = Character(name="莫妮卡", rule_system="coc")
    teammate = Character(name="亨利", rule_system="coc")
    db.add_all([module, pc, teammate])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=pc.id, status="active",
        current_scene_id="scene_office", world_state={},
    )
    db.add(session)
    db.commit()
    session_service.set_char_location(db, session.id, teammate.id, "scene_lib")
    session = db.get(GameSession, session.id)
    session_service.add_event(db, session.id, "action", "我们分头行动", actor_name="莫妮卡")
    events = session_service.get_session_events(db, session.id)

    asyncio.run(chat_service._run_generation(
        db, session.id, session, module, pc, events, teammates=[teammate],
    ))

    assert len(captured) == 2  # 两个分组各生成一次
    for _label, messages in captured:
        text = "\n".join(m["content"] for m in messages)
        assert "【本轮裁定计划】" in text
        assert "管家的秘密" in text


def test_psychology_check_is_always_blind(db_factory, monkeypatch):
    """心理学检定一律强制暗投：不挂「待玩家投骰」、不广播达成等级，结果只回灌 KP。
    其他技能（如侦查）不受影响，仍照常明骰。"""
    # 检定后 _process_commands 会触发 KP 续写；本测只关心检定本身产出的 dice chunk
    # （在续写之前 yield），把续写打桩掉以免调用真实 LLM。
    async def _no_stream(*a, **k):
        if False:
            yield None
    monkeypatch.setattr(chat_service, "_stream_narration_filtered", _no_stream)

    db = db_factory()
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(
        name="伊芙琳", rule_system="coc", is_player=True,
        skills={"心理学": 70, "侦查": 50},
    )
    db.add_all([module, hero])
    db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session)
    db.commit()

    tiers = ("大成功", "极难成功", "困难成功", "普通成功", "普通失败", "大失败")

    # 心理学：即使 KP 没写 visibility，也应强制暗投
    chunks = asyncio.run(_collect(chat_service._process_commands(
        db, session.id, "[DICE_CHECK: skill=心理学, char=伊芙琳]",
        module, hero, session, llm=None,
    )))
    assert not any('"check_request"' in c for c in chunks)   # 不挂待玩家投骰
    dice = [c for c in chunks if '"type": "dice"' in c]
    assert len(dice) == 1
    assert "暗投" in dice[0] and '"blind": true' in dice[0]
    assert not any(t in dice[0] for t in tiers)              # 聊天不含达成等级
    dice_evs = [e for e in session_service.get_session_events(db, session.id) if e.event_type == "dice"]
    assert len(dice_evs) == 1 and "结果仅 KP 可见" in dice_evs[0].content

    # 侦查：不在强制暗投名单，对真人角色仍照常挂「待玩家投骰」（与心理学的强制暗投形成对照）
    chunks2 = asyncio.run(_collect(chat_service._process_commands(
        db, session.id, "[DICE_CHECK: skill=侦查, char=伊芙琳]",
        module, hero, session, llm=None,
    )))
    assert any('"check_request"' in c for c in chunks2)
    assert not any("暗投" in c for c in chunks2)


def test_travel_runs_team_turn_so_teammates_speak(db_factory, monkeypatch):
    """玩家经大地图前往后，应紧接一轮 AI 队友回合——否则这条路（不经 run_chat_generation）
    的队友永远没有发言机会，表现为「一转移地点队友就全程哑火」。"""
    _patch_runtime(monkeypatch, db_factory)

    async def fake_stream(kp, messages, result, npcs=None, group_label=None):
        result[0] = "你推开门，眼前是一排落满灰尘的书架。"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    async def fake_process(*args, **kwargs):
        if False:
            yield None

    decided = {"n": 0}

    async def fake_decide(self, messages):
        decided["n"] += 1
        return '{"action":"speak","content":"这地方不太对劲。"}'

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)
    monkeypatch.setattr(chat_service, "_process_commands", fake_process)
    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    db = db_factory()
    module = Module(
        title="M", rule_system="coc", npcs=[],
        scenes=[{"id": "office", "name": "事务所"}, {"id": "library", "name": "图书馆"}],
    )
    hero = Character(name="莫妮卡", rule_system="coc", is_player=True)
    ai = Character(name="亨利", rule_system="coc", is_player=False)
    db.add_all([module, hero, ai])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [{"character_id": hero.id, "is_primary": True},
         {"character_id": ai.id, "role": "ai"}],
    )
    session.current_scene_id = "office"
    db.commit()

    asyncio.run(chat_service.run_travel_generation(session.id, hero.id, "library"))

    assert decided["n"] == 1  # 前往后队友确实获得了一次决策机会
    evs = session_service.get_session_events(db_factory(), session.id)
    tm_dialogues = [
        e for e in evs if e.event_type == "dialogue" and e.actor_id == ai.id
    ]
    assert len(tm_dialogues) == 1  # 队友发言落库，不再哑火
    assert tm_dialogues[0].content == "这地方不太对劲。"


def test_opening_generation_skips_turn_planner(db_factory, monkeypatch):
    """开场不是玩家行动回合，不应额外运行回合规划器。"""
    _patch_runtime(monkeypatch, db_factory)
    called = {"planner": False}

    async def fake_run_turn_planner(llm, messages):
        called["planner"] = True
        return None

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = "开场叙事"
        yield chat_service._make_chunk("narration", "开场叙事", actor_name="KP")

    monkeypatch.setattr(chat_service.turn_planner, "run_turn_planner", fake_run_turn_planner)
    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)

    asyncio.run(chat_service.run_opening_generation(session_id))

    assert called["planner"] is False


def test_opening_idempotent(db_factory, monkeypatch):
    """已有事件的会话再次触发 opening 不应重复生成。"""
    _patch_runtime(monkeypatch, db_factory)

    triggered = {"gen": False}

    async def fake_stream(kp, messages, result, npcs=None):
        triggered["gen"] = True
        result[0] = "不该发生"
        yield chat_service._make_chunk("narration", "不该发生", actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    db = db_factory()
    session_id = _seed_session(db)
    session_service.add_event(db, session_id, "narration", "已有开局", actor_name="KP")

    asyncio.run(chat_service.run_opening_generation(session_id))

    assert triggered["gen"] is False
    assert len(_narrations(db_factory, session_id)) == 1


def test_trailing_pronoun_speaker_dialogue_extracted():
    """说话人后置且用代词（"台词，"她说）也要抽成气泡——判不出具名时兜底署名代词，
    好过把台词混进旁白。"""
    text = "她望着你，浅浅一笑。\n\n“你是那位医生吧，”她说，语气温和。她站起身。"
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=[{"name": "霍尔护士长"}])
    ))
    texts = [t for _, t in result[2]]
    speakers = [s for s, _ in result[2]]
    assert any("你是那位医生吧" in t for t in texts)
    assert "她" in speakers


def test_trailing_named_speaker_dialogue_extracted():
    """说话人后置且具名（"台词。"霍尔护士长说道）→ 气泡署名具名 NPC。"""
    text = "门开了。\n\n“请坐。”霍尔护士长说道，指了指椅子。"
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=[{"name": "霍尔护士长"}])
    ))
    assert result[2] and result[2][0][0] == "霍尔护士长"
    assert "请坐" in result[2][0][1]


def test_written_quote_not_deferred_as_dialogue():
    """书写/标识内容（写着「…」）不进后置说话人判定，仍留旁白。"""
    text = "门上写着“禁止入内”。他皱了皱眉。"
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=[{"name": "某人"}])
    ))
    assert result[2] == []
    assert "禁止入内" in result[0]


def test_quote_without_trailing_verb_stays_narration():
    """引号后没有紧邻的说话动词 → 判不出说话人时不硬抽，原样留旁白。"""
    text = "“咚。”门缓缓合上了，走廊恢复了安静，再无人声。"
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=[{"name": "某人"}])
    ))
    assert result[2] == []
    assert "咚" in result[0]
