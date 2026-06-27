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


def test_parse_team_decision():
    assert chat_service._parse_team_decision('{"action":"act","content":"查看"}') == {
        "action": "act",
        "content": "查看",
    }
    assert chat_service._parse_team_decision("前缀 {\"action\":\"speak\",\"content\":\"嗨\"} 后缀") == {
        "action": "speak",
        "content": "嗨",
    }
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
    assert "同场的其他玩家角色" in system
    assert "阿尔法" in system and "贝塔" in system

    # 队友发言进入 user 侧、带「队友·」前缀，不会被误判成 KP 的 assistant 输出
    joined_user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    assert "[队友·阿尔法]" in joined_user


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
