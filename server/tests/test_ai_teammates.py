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
