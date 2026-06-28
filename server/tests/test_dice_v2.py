"""骰子 v2 回归：目标角色（队友/NPC 检定）、暗投/暗骰、对抗骰、NPC 数值卡兜底。

掷骰随机，故只断言「对谁投/是否暗骰/是否对抗/胜负字段」等结构，不断言具体成败。
KP 续写用 fake 桩避免真实 LLM。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (  # noqa: F401 注册表
    Base, Character, EventLog, GameSession, Module, SessionParticipant,
)
from app.services import chat_service, session_service


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'd.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="陵墓", rule_system="coc", scenes=[], clues=[],
        npcs=[{"id": "g", "name": "守墓人", "skills": {"潜行": 70, "战斗": 55}}],
    )
    hero = Character(name="主角", rule_system="coc", is_player=True,
                     skills={"侦查": 60, "心理学": 50})
    ally = Character(name="阿尔法", rule_system="coc", is_player=False,
                     skills={"图书馆使用": 65})
    db.add_all([module, hero, ally])
    db.commit()
    session = GameSession(module_id=module.id, player_character_id=hero.id, status="active")
    db.add(session)
    db.commit()
    return module, hero, [ally], session


def _dice(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: "):
            d = json.loads(c[6:])
            if d.get("type") == "dice":
                out.append(d)
    return out


def _run(db, module, hero, teammates, session, kp_text, monkeypatch):
    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = ""
        result[1] = ""
        return
        yield  # noqa — 使其成为 async generator

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    async def go():
        chunks = []
        async for ch in chat_service._process_commands(
            db, session.id, kp_text, module, hero, session, None, teammates=teammates,
        ):
            chunks.append(ch)
        return chunks

    return asyncio.run(go())


def test_parse_tag_kv_and_resolve_actor(db_factory):
    db = db_factory()
    module, hero, teammates, _ = _seed(db)
    assert chat_service._parse_tag_kv("skill=侦查, char=守墓人, visibility=blind") == {
        "skill": "侦查", "char": "守墓人", "visibility": "blind",
    }
    # 主角
    cd, name, is_npc = chat_service._resolve_check_actor("", "侦查", hero, teammates, module)
    assert name == "主角" and is_npc is False and cd["skills"]["侦查"] == 60
    # 队友
    cd, name, is_npc = chat_service._resolve_check_actor("阿尔法", "图书馆使用", hero, teammates, module)
    assert name == "阿尔法" and is_npc is False
    # NPC + 缺失技能用基线兜底
    cd, name, is_npc = chat_service._resolve_check_actor("守墓人", "聆听", hero, teammates, module)
    assert name == "守墓人" and is_npc is True
    assert cd["skills"]["聆听"] == chat_service.DEFAULT_NPC_SKILL  # 兜底
    assert cd["skills"]["潜行"] == 70                              # 卡上的保留


def test_player_and_teammate_open_check(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    chunks = _run(
        db, module, hero, teammates, session,
        "你仔细搜索。\n[DICE_CHECK: skill=侦查, difficulty=normal]", monkeypatch,
    )
    dice = _dice(chunks)
    assert len(dice) == 1
    assert dice[0]["metadata"]["actor"] == "主角"
    assert "blind" not in dice[0]["metadata"]

    chunks = _run(
        db, module, hero, teammates, session,
        "[DICE_CHECK: skill=图书馆使用, char=阿尔法]", monkeypatch,
    )
    assert _dice(chunks)[0]["metadata"]["actor"] == "阿尔法"


def test_blind_player_and_npc(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    # 玩家暗投
    d = _dice(_run(db, module, hero, teammates, session,
                   "[DICE_CHECK: skill=心理学, char=主角, visibility=blind]", monkeypatch))[0]
    assert d["metadata"]["blind"] is True
    assert "暗投" in d["content"]
    assert "成功" not in d["content"] and "失败" not in d["content"]  # 不泄露成败
    assert "outcome" not in d["metadata"]

    # NPC 暗骰
    d = _dice(_run(db, module, hero, teammates, session,
                   "[DICE_CHECK: skill=潜行, char=守墓人, visibility=blind]", monkeypatch))[0]
    assert d["metadata"]["blind"] is True and d["metadata"]["actor"] == "守墓人"
    assert "暗骰" in d["content"]


def test_run_check_generation_rolls_for_actor(db_factory, monkeypatch):
    """玩家主动检定：run_check_generation 对其角色掷骰并落 dice 事件，再交 KP 续写。"""
    import app.database as database
    from app.services.room_hub import room_hub

    db = db_factory()
    module, hero, teammates, session = _seed(db)  # hero 有 侦查=60

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_service, "get_llm", lambda: None)
    monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = ""
        result[1] = ""
        return
        yield

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    import asyncio as _asyncio
    _asyncio.run(chat_service.run_check_generation(session.id, hero.id, "侦查", "normal"))

    dice = [e for e in session_service.get_session_events(db_factory(), session.id)
            if e.event_type == "dice"]
    assert len(dice) == 1
    assert dice[0].metadata_["actor"] == hero.name
    assert dice[0].metadata_["skill"] == "侦查"
    assert dice[0].metadata_["skill_value"] == 60


def test_opposed_check(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[OPPOSED_CHECK: a=主角, a_skill=潜行, b=守墓人, b_skill=侦查]", monkeypatch))[0]
    assert d["metadata"]["opposed"] is True
    assert d["metadata"]["winner"] in ("主角", "守墓人", "平局")
    assert "对抗骰" in d["content"]
