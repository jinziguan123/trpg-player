"""AI 队友自动响应编排的回归测试。

只验证编排边界，不依赖真实 LLM：
1. 玩家输入后队友自动回合触发，每个队友只响应一次。
2. 队友不会把自己再递归触发成下一轮（decide 调用次数 == 队友数）。
3. 解析失败 / silent 时 hold，不落库。
4. build_kp_context 能把整个队伍写进提示词，且队友发言算 user 侧输入。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.models import (  # noqa: F401 — 注册全部表
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc", is_player=True)
    a1 = Character(name="阿尔法", rule_system="coc", is_player=False)
    a2 = Character(name="贝塔", rule_system="coc", is_player=False)
    db.add_all([module, hero, a1, a2])
    db.commit()
    session = session_service.create_session(
        db,
        module.id,
        [
            {"character_id": hero.id, "is_primary": True},
            {"character_id": a1.id, "role": "ai"},
            {"character_id": a2.id, "role": "ai"},
        ],
    )
    return module, hero, [a1, a2], session


async def _collect(agen):
    return [c async for c in agen]


def test_team_turn_runs_once_per_teammate(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    session_service.add_event(
        db, session.id, "dialogue", "我推开门", actor_id=hero.id, actor_name=hero.name,
    )

    calls = {"n": 0}

    async def fake_decide(self, messages):
        calls["n"] += 1
        return '{"action": "speak", "content": "小心点！"}'

    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    chunks = asyncio.run(
        _collect(
            chat_service._run_team_turn(
                db, session.id, session, module, hero, teammates, llm=None,
            )
        )
    )

    # 两个队友各决策一次，绝不超过队友数（无递归自触发）
    assert calls["n"] == 2
    # 两条队友发言入库
    dialogues = [
        e
        for e in session_service.get_session_events(db, session.id)
        if e.event_type == "dialogue" and e.actor_id in {t.id for t in teammates}
    ]
    assert len(dialogues) == 2
    # 走前端气泡的 npc_dialogue chunk
    assert sum('"npc_dialogue"' in c for c in chunks) == 2


def test_team_turn_check_rolls_dice(db_factory, monkeypatch):
    """队友 check 决策：先落 action，再紧接着掷骰（dice 事件，带技能元数据）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)

    async def fake_decide(self, messages):
        return '{"action":"check","content":"我辨认铭文","skill":"考古学"}'

    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    chunks = asyncio.run(_collect(
        chat_service._run_team_turn(
            db, session.id, session, module, hero, teammates, llm=None,
        )
    ))
    events = session_service.get_session_events(db, session.id)
    tm_ids = {t.id for t in teammates}
    actions = [e for e in events if e.event_type == "action" and e.actor_id in tm_ids]
    dice = [e for e in events if e.event_type == "dice"]
    assert len(actions) == 2 and len(dice) == 2          # 每个队友 1 action + 1 dice
    assert all(d.metadata_.get("skill") == "考古学" for d in dice)
    assert sum('"dice"' in c for c in chunks) == 2


def test_team_psychology_check_is_blind(db_factory, monkeypatch):
    """队友主动做心理学检定：只落/广播「做了一次暗骰」，不含成败；真实结果仅回灌 blind_results
    （注入当轮 KP 上下文，绝不落库/广播——否则玩家能从事件或网络看到结果而元游戏）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    for t in teammates:
        t.skills = {"心理学": 60}
    db.commit()

    async def fake_decide(self, messages):
        return '{"action":"check","content":"我观察他的微表情","skill":"心理学"}'

    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    blind: list[str] = []
    chunks = asyncio.run(_collect(chat_service._run_team_turn(
        db, session.id, session, module, hero, teammates, llm=None, blind_results=blind,
    )))

    tiers = ("大成功", "极难成功", "困难成功", "普通成功", "普通失败", "大失败")
    dice_chunks = [c for c in chunks if '"dice"' in c]
    assert len(dice_chunks) == 2
    assert all("暗骰" in c for c in dice_chunks)
    assert all(not any(t in c for t in tiers) for c in dice_chunks)   # 广播不含成败

    dice_evs = [e for e in session_service.get_session_events(db, session.id) if e.event_type == "dice"]
    assert len(dice_evs) == 2
    for e in dice_evs:
        assert "结果仅 KP 可见" in e.content
        assert not any(t in e.content for t in tiers)                 # 落库不含成败
        assert (e.metadata_ or {}).get("outcome") is None            # metadata 不泄露结果
        assert (e.metadata_ or {}).get("blind") is True

    # 真实结果只回灌 KP（当轮上下文用，不落库/广播）
    assert len(blind) == 2 and all("仅你 KP 可见" in b for b in blind)


def test_team_turn_holds_on_silent_and_bad_json(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)

    async def fake_decide(self, messages):
        if self.character_id == teammates[0].id:
            return '{"action": "silent", "content": ""}'
        return "这不是合法 JSON"

    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    asyncio.run(
        _collect(
            chat_service._run_team_turn(
                db, session.id, session, module, hero, teammates, llm=None,
            )
        )
    )

    teammate_events = [
        e
        for e in session_service.get_session_events(db, session.id)
        if e.actor_id in {t.id for t in teammates}
    ]
    assert teammate_events == []  # silent + 解析失败都 hold，不落库


def test_team_context_switches_guidance_by_separation(db_factory):
    """同处一地 → 克制补位（宁缺毋滥）；分头独处 → 主动推进本场景（全靠自己）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    together = ctx.build_team_context(
        teammates[0], session, module, events, hero,
        all_teammates=teammates, separated=False,
    )[0]["content"]
    apart = ctx.build_team_context(
        teammates[0], session, module, events, hero,
        all_teammates=teammates, separated=True,
    )[0]["content"]

    assert "补位与响应" in together and "宁缺毋滥" in together
    assert "全靠你自己" in apart and "主动" in apart
    assert "补位与响应" not in apart  # 分头时不再是补位定位


def test_team_turn_marks_separated_teammate_proactive(db_factory, monkeypatch):
    """_run_team_turn 按各队友「所在场景 vs 主队锚点」判定分头：分头者收到主动推进指引，
    同处者收到克制补位指引。"""
    db = db_factory()
    module = Module(
        title="宅邸", rule_system="coc", npcs=[],
        scenes=[{"id": "hall", "name": "大厅"}, {"id": "cellar", "name": "地窖"}],
    )
    hero = Character(name="主角", rule_system="coc", is_player=True)
    a1 = Character(name="阿尔法", rule_system="coc", is_player=False)
    a2 = Character(name="贝塔", rule_system="coc", is_player=False)
    db.add_all([module, hero, a1, a2])
    db.commit()
    session = session_service.create_session(
        db, module.id,
        [{"character_id": hero.id, "is_primary": True},
         {"character_id": a1.id, "role": "ai"},
         {"character_id": a2.id, "role": "ai"}],
    )
    session.current_scene_id = "hall"
    db.commit()
    session_service.set_char_location(db, session.id, hero.id, "hall")
    session_service.set_char_location(db, session.id, a1.id, "cellar")  # 分头独处
    session_service.set_char_location(db, session.id, a2.id, "hall")    # 与主队同处
    session = db.get(GameSession, session.id)

    seen: dict[str, str] = {}

    async def fake_decide(self, messages):
        seen[self.character_id] = messages[0]["content"]
        return '{"action": "silent", "content": ""}'

    monkeypatch.setattr(chat_service.TeamAgent, "decide", fake_decide)

    asyncio.run(_collect(chat_service._run_team_turn(
        db, session.id, session, module, hero, [a1, a2], llm=None,
    )))

    assert "全靠你自己" in seen[a1.id]     # 分头 → 主动推进
    assert "补位与响应" in seen[a2.id]     # 同处 → 克制补位


def test_rollback_last_kp_output_keeps_inputs_and_dice(db_factory):
    """重新生成的回滚：删掉最新一轮 KP 叙事产物（旁白/NPC 台词/待投骰请求），
    保留玩家与队友的输入、以及已投出的骰子结果（不重掷）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    a1 = teammates[0]

    # 一轮：玩家行动 → 队友发言 → 队友检定骰 → KP 旁白 → NPC 台词 → 检定骰 → 待投骰请求
    session_service.add_event(db, session.id, "action", "我推开门", actor_id=hero.id, actor_name=hero.name)
    session_service.add_event(db, session.id, "dialogue", "小心点", actor_id=a1.id, actor_name=a1.name)
    session_service.add_event(db, session.id, "dice", "阿尔法｜考古学：成功", actor_name="系统", metadata={"skill": "考古学"})
    session_service.add_event(db, session.id, "narration", "门后是一条走廊。", actor_name="KP")
    session_service.add_event(db, session.id, "dialogue", "谁在那儿？", actor_name="老管家")  # NPC：actor_id 为 None
    session_service.add_event(db, session.id, "dice", "亨利｜侦查：失败", actor_name="系统", metadata={"skill": "侦查"})
    session_service.add_pending_check(db, session.id, {"id": "chk1", "skill": "聆听"})
    session_service.add_event(
        db, session.id, "system", "请 亨利 进行一次「聆听」检定",
        actor_name="系统", metadata={"check_request": True, "id": "chk1"},
    )

    removed = session_service.rollback_last_kp_output(db, session.id)
    assert removed == 3  # narration + NPC 台词 + 待投骰请求

    evs = session_service.get_session_events(db, session.id)
    sig = [(e.event_type, e.actor_name) for e in evs]
    assert ("narration", "KP") not in sig
    assert ("dialogue", "老管家") not in sig
    assert not any(e.event_type == "system" and (e.metadata_ or {}).get("check_request") for e in evs)
    # 保留：玩家/队友输入 + 两条骰子（已定，不重掷）
    assert ("action", hero.name) in sig
    assert ("dialogue", a1.name) in sig
    assert sum(1 for e in evs if e.event_type == "dice") == 2
    # 待投骰请求对应的 pending_check 也被清掉
    sess = db.get(GameSession, session.id)
    assert not (sess.world_state or {}).get("pending_checks")


def test_old_events_summary_keeps_recent_not_opening():
    """长局历史摘要应保留「离当前最近」的老事件，而非停在最早的开场——否则 KP 记忆停滞、
    原地打转、复读开场式内容。"""
    from types import SimpleNamespace

    events = [
        SimpleNamespace(summary=None, actor_name="KP", event_type="narration", content=f"第{i}段剧情")
        for i in range(200)
    ]
    out = ctx._summarize_old_events(events, max_tokens=120)
    assert out  # 非空
    assert "第199段剧情" in out          # 保留最近
    assert "第0段剧情" not in out         # 丢掉最早的开场
    # 输出保持时间正序（较早的行在前）
    assert out.index("第198段剧情") < out.index("第199段剧情")


def test_story_summarizer_merges_and_fails_open():
    """滚动摘要生成：正常返回浓缩文本；无 LLM / 无事件 / 调用异常一律 None（保持原摘要）。"""
    from types import SimpleNamespace

    from app.ai import story_summarizer

    class LLM:
        async def complete(self, messages, **kw):
            return "梗概：调查员到过疗养院。"

    evs = [SimpleNamespace(event_type="narration", actor_name="KP", content="到了疗养院")]
    assert asyncio.run(story_summarizer.summarize_story(LLM(), "旧摘要", evs)) == "梗概：调查员到过疗养院。"
    assert asyncio.run(story_summarizer.summarize_story(None, "x", evs)) is None
    assert asyncio.run(story_summarizer.summarize_story(LLM(), "x", [])) is None

    class BadLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    assert asyncio.run(story_summarizer.summarize_story(BadLLM(), "x", evs)) is None


def test_maybe_roll_story_summary_updates_and_advances_cursor(db_factory, monkeypatch):
    """未并入摘要的事件超过阈值时，把较老的一批浓缩进持久摘要并推进游标；不足阈值则不动。"""
    # v2：滚动点改走「摘要 + 记忆抽取」合并调用；桩返回三元组，差量为空只验摘要滚动。
    async def fake_summarize(llm, prev, events, npc_brief):
        return ("合并后的滚动摘要", {}, {})

    monkeypatch.setattr(
        chat_service.story_summarizer, "summarize_and_extract", fake_summarize,
    )

    db = db_factory()
    module, hero, teammates, session = _seed(db)
    for i in range(30):  # > STORY_SUMMARY_TRIGGER(24)
        session_service.add_event(db, session.id, "narration", f"第{i}段", actor_name="KP")

    asyncio.run(chat_service._maybe_roll_story_summary(db, session.id, llm=object()))
    ws = (db.get(GameSession, session.id).world_state) or {}
    assert ws.get("story_summary") == "合并后的滚动摘要"
    cursor = ws.get("story_summary_seq")
    assert cursor
    events = session_service.get_session_events(db, session.id, limit=0)
    uncovered = [e for e in events if (e.sequence_num or 0) > cursor]
    assert len(uncovered) == chat_service.STORY_SUMMARY_KEEP_RECENT  # 只剩最近这些未并入

    # 事件不足阈值 → 不滚动
    db2 = db_factory()
    _m, _h, _t, s2 = _seed(db2)
    for i in range(10):
        session_service.add_event(db2, s2.id, "narration", f"x{i}", actor_name="KP")
    asyncio.run(chat_service._maybe_roll_story_summary(db2, s2.id, llm=object()))
    assert not (db2.get(GameSession, s2.id).world_state or {}).get("story_summary")


def test_kp_context_uses_persistent_story_summary(db_factory):
    """build_kp_context：注入持久滚动摘要，且被游标覆盖的老事件不再逐条进上下文；
    游标之后的最近事件仍照常给全文。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    for i in range(5):
        session_service.add_event(db, session.id, "narration", f"覆盖段{i}", actor_name="KP")
    for i in range(3):
        session_service.add_event(db, session.id, "action", f"最近行动{i}", actor_id=hero.id, actor_name=hero.name)
    events = session_service.get_session_events(db, session.id, limit=0)
    covered_seq = events[4].sequence_num  # 覆盖前 5 条 narration
    session.world_state = {"story_summary": "【梗概】前情提要在此。", "story_summary_seq": covered_seq}
    db.commit()

    msgs = ctx.build_kp_context(session, module, hero, events, teammates=teammates)
    joined = "\n".join(m["content"] for m in msgs)
    assert "前情提要在此" in joined     # 持久摘要注入
    assert "覆盖段0" not in joined       # 被游标覆盖的老事件不再逐条进上下文
    assert "最近行动2" in joined         # 游标之后的最近事件仍在


def test_parse_team_decision():
    assert chat_service._parse_team_decision('{"action":"act","content":"查看"}') == {
        "action": "act",
        "content": "查看",
        "skill": "",
        "target": "",
    }
    assert chat_service._parse_team_decision("前缀 {\"action\":\"speak\",\"content\":\"嗨\"} 后缀") == {
        "action": "speak",
        "content": "嗨",
        "skill": "",
        "target": "",
    }
    # check 行动带 skill
    assert chat_service._parse_team_decision(
        '{"action":"check","content":"我辨认铭文","skill":"考古学"}'
    ) == {"action": "check", "content": "我辨认铭文", "skill": "考古学", "target": ""}
    # travel 行动带 target
    assert chat_service._parse_team_decision(
        '{"action":"travel","content":"我去图书馆","target":"中央图书馆"}'
    ) == {"action": "travel", "content": "我去图书馆", "skill": "", "target": "中央图书馆"}
    assert chat_service._parse_team_decision("坏数据") is None
    assert chat_service._parse_team_decision('{"action":"unknown","content":"x"}') is None


def test_kp_context_includes_party(db_factory):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    session_service.add_event(
        db, session.id, "dialogue", "我推开门", actor_id=hero.id, actor_name=hero.name,
    )
    session_service.add_event(
        db, session.id, "dialogue", "我殿后", actor_id=teammates[0].id,
        actor_name=teammates[0].name,
    )
    events = session_service.get_session_events(db, session.id)

    messages = ctx.build_kp_context(session, module, hero, events, teammates=teammates)
    system = messages[0]["content"]
    # 统一的队伍名册（一视同仁，无主角特权）：房主角色与队友都在册
    assert "地位完全平等" in system
    assert "阿尔法" in system and "贝塔" in system and hero.name in system

    # 所有玩家角色发言统一以「[名字]」进入 user 侧（无主角裸渲染、无"队友·"特权区分）
    joined_user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    assert "[阿尔法]" in joined_user and f"[{hero.name}]" in joined_user
    assert "队友·" not in joined_user


def test_kp_context_uses_viewer_scene_for_split(db_factory):
    """分头行动：build_kp_context 按 viewer_scene_id 给出「该组所在场景」的 NPC，而非只有主角
    场景的——否则每一列都拿主角场景资料，KP 只能把主角场景重复叙述一遍（两列讲同一件事）。"""
    db = db_factory()
    module = Module(
        title="宅邸", rule_system="coc",
        scenes=[
            {"id": "ward", "name": "疗养院", "description": "消毒水气味"},
            {"id": "archive", "name": "档案馆", "description": "落满灰尘的书架"},
        ],
        npcs=[
            {"id": "nurse", "name": "护士长", "description": "值班护士", "initial_location": "ward"},
            {"id": "clerk", "name": "档案管理员", "description": "戴眼镜的老人", "initial_location": "archive"},
        ],
    )
    hero = Character(name="伊芙琳", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id, status="active",
        current_scene_id="ward", world_state={"visited_scenes": ["ward"]},
    )
    db.add(session)
    db.commit()
    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="护士长走了过来。", actor_name="KP")

    # 主角所在（ward）视角：只见护士长，不见档案馆的 NPC
    sys_ward = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "护士长" in sys_ward and "档案管理员" not in sys_ward

    # 档案馆分组视角（viewer_scene_id=archive）：能看到本场景 NPC 档案管理员
    sys_arch = ctx.build_kp_context(
        session, module, hero, [ev], viewer_scene_id="archive",
    )[0]["content"]
    assert "档案管理员" in sys_arch


def test_opening_context_hides_discoverables(db_factory):
    """开场上下文：只给起始场景 NPC、剥 secrets、不给线索；游戏中恢复完整资料。"""
    db = db_factory()
    module = Module(
        title="陵墓", rule_system="coc",
        scenes=[{"id": "entrance", "name": "入口", "description": "沙漠中的墓门"}],
        npcs=[
            {"id": "g", "name": "老向导", "description": "当地贝都因人",
             "secrets": "知道附近有水源", "initial_location": "entrance"},
            {"id": "s", "name": "萨沙·卡纳", "description": "失踪的德国人类学家",
             "secrets": "尸体在耳室", "initial_location": "side_chamber"},
        ],
        clues=[{"id": "c", "name": "萨沙的笔记", "description": "记载了密道坐标",
                "location": "side_chamber"}],
    )
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id,
        status="active", current_scene_id="entrance",
    )
    db.add(session)
    db.commit()

    sys_open = ctx.build_kp_context(session, module, hero, [])[0]["content"]
    assert "老向导" in sys_open            # 起始场景 NPC 保留
    assert "萨沙·卡纳" not in sys_open      # 深处 NPC 不出现在开场
    assert "尸体在耳室" not in sys_open     # NPC secrets 剥离
    assert "知道附近有水源" not in sys_open  # 起始 NPC 的 secret 也剥离
    assert "密道坐标" not in sys_open       # 线索内容开场不给

    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="开场已生成", actor_name="KP")
    # 游戏中：尚未访问 side_chamber，深处 NPC / 线索仍不进入 KP 上下文（1-C 分层）
    sys_play = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "老向导" in sys_play             # 已访问的起始场景 NPC 在场
    assert "萨沙·卡纳" not in sys_play       # 未到达区域的 NPC 仍不泄露
    assert "密道坐标" not in sys_play        # 未到达区域的线索仍不泄露

    # 玩家探索到 side_chamber 后，该区域的 NPC / 线索才进入 KP 上下文
    session.current_scene_id = "side_chamber"
    session.world_state = {"visited_scenes": ["entrance", "side_chamber"]}
    sys_deep = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "萨沙·卡纳" in sys_deep
    assert "密道坐标" in sys_deep


def test_player_brief_used_as_opening_hook(db_factory):
    """1-A：player_brief 作为开场唯一合法钩子；无则不强加。"""
    db = db_factory()
    brief = "你是受雇于波士顿古物商的私家侦探，受托去阿卡姆调查一批失窃的文物。"
    module = Module(
        title="失窃案", rule_system="coc",
        scenes=[{"id": "office", "name": "事务所", "description": "昏暗的办公室"}],
        npcs=[], clues=[],
        world_setting={"player_brief": brief},
    )
    hero = Character(name="侦探", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id,
        status="active", current_scene_id="office",
    )
    db.add(session)
    db.commit()

    # “除此之外，玩家此刻一无所知” 是注入钩子独有的标记（不在静态开场提示里）
    HOOK_MARK = "除此之外，玩家此刻一无所知"
    msgs = ctx.build_kp_context(session, module, hero, [])
    opening = "\n".join(m["content"] for m in msgs if m["role"] == "user")
    assert HOOK_MARK in opening
    assert "受托去阿卡姆" in opening

    # 无 player_brief 时不强加钩子
    module.world_setting = {}
    msgs2 = ctx.build_kp_context(session, module, hero, [])
    opening2 = "\n".join(m["content"] for m in msgs2 if m["role"] == "user")
    assert HOOK_MARK not in opening2
