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


def _of_type(chunks, t):
    out = []
    for c in chunks:
        if c.startswith("data: "):
            d = json.loads(c[6:])
            if d.get("type") == t:
                out.append(d)
    return out


def _dice(chunks):
    return _of_type(chunks, "dice")


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
    cd, name, is_npc, cid = chat_service._resolve_check_actor("", "侦查", hero, teammates, module)
    assert name == "主角" and is_npc is False and cd["skills"]["侦查"] == 60 and cid == hero.id
    # 队友
    cd, name, is_npc, cid = chat_service._resolve_check_actor("阿尔法", "图书馆使用", hero, teammates, module)
    assert name == "阿尔法" and is_npc is False and cid == teammates[0].id
    # NPC + 缺失技能用基线兜底
    cd, name, is_npc, cid = chat_service._resolve_check_actor("守墓人", "聆听", hero, teammates, module)
    assert name == "守墓人" and is_npc is True and cid is None
    assert cd["skills"]["聆听"] == chat_service.DEFAULT_NPC_SKILL  # 兜底
    assert cd["skills"]["潜行"] == 70                              # 卡上的保留


def test_player_check_pends_for_manual_roll(db_factory, monkeypatch):
    """真人主角的明骰检定 → 不自动掷，挂成「待玩家投骰」并给出提示。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    chunks = _run(
        db, module, hero, teammates, session,
        "你仔细搜索。\n[DICE_CHECK: skill=侦查, difficulty=hard]", monkeypatch,
    )
    assert _dice(chunks) == []                     # 不自动掷
    reqs = _of_type(chunks, "check_request")
    assert len(reqs) == 1
    assert reqs[0]["metadata"]["skill"] == "侦查"
    assert reqs[0]["metadata"]["difficulty"] == "hard"
    assert "困难" in reqs[0]["content"] and "侦查" in reqs[0]["content"]
    # 待定检定已登记，等 /roll
    db.expire_all()
    pending = (db.get(GameSession, session.id).world_state or {}).get("pending_checks") or {}
    assert any(c["skill"] == "侦查" for c in pending.values())


def test_ai_teammate_check_auto_rolls(db_factory, monkeypatch):
    """AI 队友（非真人控制）的检定仍由系统自动掷。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    chunks = _run(
        db, module, hero, teammates, session,
        "[DICE_CHECK: skill=图书馆使用, char=阿尔法]", monkeypatch,
    )
    dice = _dice(chunks)
    assert len(dice) == 1 and dice[0]["metadata"]["actor"] == "阿尔法"
    assert dice[0]["metadata"]["tier"] in (
        "critical", "extreme", "hard", "regular", "fail", "fumble",
    )


def test_check_prompt_phrasing():
    """req 1：普通难度不带难度词；困难/极难带难度词。"""
    assert chat_service._check_prompt_text("张三", "侦查", "normal") == "请 张三 进行一次「侦查」检定"
    assert "困难" in chat_service._check_prompt_text("张三", "侦查", "hard")
    assert "极难" in chat_service._check_prompt_text("张三", "图书馆使用", "extreme")


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


def test_roll_generation_rolls_pending_check(db_factory, monkeypatch):
    """玩家点投骰：run_roll_generation 取出待定检定、掷骰落 dice 事件（含达成等级），再交 KP 续写。"""
    import app.database as database
    from app.services.room_hub import room_hub

    db = db_factory()
    module, hero, teammates, session = _seed(db)  # hero 有 侦查=60
    session_service.add_pending_check(db, session.id, {
        "id": "chk1", "skill": "侦查", "difficulty": "normal",
        "char_ref": "", "char_id": hero.id, "actor_name": hero.name, "source": "",
    })

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_service, "get_llm", lambda: None)
    monkeypatch.setattr(chat_service, "get_fast_llm", lambda: None)
    monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = ""
        result[1] = ""
        return
        yield

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    import asyncio as _asyncio
    _asyncio.run(chat_service.run_roll_generation(session.id, "chk1"))

    fresh = db_factory()
    dice = [e for e in session_service.get_session_events(fresh, session.id)
            if e.event_type == "dice"]
    assert len(dice) == 1
    assert dice[0].metadata_["actor"] == hero.name
    assert dice[0].metadata_["skill"] == "侦查"
    assert dice[0].metadata_["skill_value"] == 60
    assert dice[0].metadata_["tier"] in (
        "critical", "extreme", "hard", "regular", "fail", "fumble",
    )
    # 待定检定已被消费
    pending = (fresh.get(GameSession, session.id).world_state or {}).get("pending_checks") or {}
    assert "chk1" not in pending


def test_dice_continuation_sanity_guard_only_on_success(db_factory, monkeypatch):
    """SAN 守卫成本收窄：检定**成功**续写才补跑 planner 判理智；**失败**不多跑（省调用）。"""
    import asyncio as _asyncio

    import app.database as database
    from app.ai import turn_planner
    from app.ai.turn_planner import TurnPlan
    from app.services.room_hub import room_hub

    called = {"n": 0}

    async def spy_planner(llm, messages):
        called["n"] += 1
        return TurnPlan()   # trigger=False：只观测是否被调用，不实际发 SAN

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = result[1] = "旁白。"
        return
        yield

    async def noop_finish(db, sid, llm):
        return None

    def _drive(roll: int) -> int:
        db = db_factory()
        module, hero, teammates, session = _seed(db)   # hero 侦查=60
        session_service.add_pending_check(db, session.id, {
            "id": "chk1", "skill": "侦查", "difficulty": "normal",
            "char_ref": "", "char_id": hero.id, "actor_name": hero.name, "source": "",
        })
        monkeypatch.setattr(database, "SessionLocal", db_factory)
        monkeypatch.setattr(chat_service, "get_llm", lambda: None)
        monkeypatch.setattr(chat_service, "get_fast_llm", lambda: None)
        monkeypatch.setattr(chat_service, "KPAgent", lambda llm: object())
        monkeypatch.setattr(room_hub, "broadcast", lambda *a, **k: None)
        monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)
        monkeypatch.setattr(chat_service, "build_kp_context", lambda *a, **k: [{"role": "system", "content": "x"}])
        monkeypatch.setattr(chat_service, "_module_excerpts_for_context", lambda *a, **k: [])
        monkeypatch.setattr(chat_service, "_finish_generation", noop_finish)
        monkeypatch.setattr(turn_planner, "run_turn_planner", spy_planner)
        monkeypatch.setattr("app.rules.coc.checks.roll_percentile", lambda: roll)
        called["n"] = 0
        _asyncio.run(chat_service.run_roll_generation(session.id, "chk1"))
        return called["n"]

    assert _drive(10) == 1    # 成功(10≤60) → 补跑 planner 判理智
    assert _drive(99) == 0    # 失败/大失败 → 不多跑，省成本


def test_dice_continuation_fires_followup_san_check(db_factory, monkeypatch):
    """检定续写里 KP 追加的 [SAN_CHECK]（如读懂禁忌知识）应被处理、落 SAN 检定事件。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    hero.system_data = {"sanity": {"current": 50, "max": 99}}
    db.commit()

    calls = {"n": 0}

    async def fake_stream(kp, messages, result, npcs=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一段续写：揭示禁忌知识并在末尾追加 SAN 检定
            result[0] = "你读懂了亵渎的铭文，领悟了不该知道的真相。"
            result[1] = result[0] + "\n[SAN_CHECK: success_loss=0, failure_loss=1d4]"
        else:
            result[0] = "你的精神受到冲击。"
            result[1] = result[0]
        return
        yield

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    async def go():
        # 用 NPC 检定触发自动掷→续写链（真人主角检定改为待手动投骰，不会即时续写）
        return [c async for c in chat_service._process_commands(
            db, session.id, "守墓人凝视着铭文……\n[DICE_CHECK: skill=侦查, char=守墓人]",
            module, hero, session, None, teammates=teammates,
        )]

    asyncio.run(go())

    events = session_service.get_session_events(db, session.id)
    san_dice = [e for e in events if e.event_type == "dice" and e.metadata_.get("skill") == "SAN"]
    assert len(san_dice) >= 1, "续写里的 SAN_CHECK 应被处理并落 SAN 检定事件"


def test_san_per_character_and_once_per_source(db_factory, monkeypatch):
    """SAN 各自结算 + 同一角色对同一恐怖源只检定一次（晚到/新恐怖才再检）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    ally = teammates[0]
    hero.system_data = {"sanity": {"current": 60, "max": 99}}
    ally.system_data = {"sanity": {"current": 55, "max": 99}}
    db.commit()

    async def fake_stream(kp, messages, result, npcs=None):
        result[0] = ""
        result[1] = ""
        return
        yield

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    def san_events():
        return [e for e in session_service.get_session_events(db, session.id)
                if e.event_type == "dice" and e.metadata_.get("skill") == "SAN"]

    def run(text):
        async def go():
            return [c async for c in chat_service._process_commands(
                db, session.id, text, module, hero, session, None, teammates=teammates,
            )]
        return asyncio.run(go())

    # 主角+阿尔法同时目睹尸体 → 各自一次 SAN
    run("[SAN_CHECK: success_loss=0, failure_loss=1d2, chars=主角/阿尔法, source=尸体]")
    assert {e.metadata_["actor"] for e in san_events()} == {"主角", "阿尔法"}

    # 再次对同一尸体（同 source）→ 两人都已检定 → 不再新增
    run("[SAN_CHECK: success_loss=0, failure_loss=1d2, chars=主角/阿尔法, source=尸体]")
    assert len(san_events()) == 2

    # 全新恐怖源 → 主角再检定一次
    run("[SAN_CHECK: success_loss=0, failure_loss=1d2, chars=主角, source=怪物]")
    assert len(san_events()) == 3


def test_opposed_check(db_factory, monkeypatch):
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    d = _dice(_run(db, module, hero, teammates, session,
                   "[OPPOSED_CHECK: a=主角, a_skill=潜行, b=守墓人, b_skill=侦查]", monkeypatch))[0]
    assert d["metadata"]["opposed"] is True
    assert d["metadata"]["winner"] in ("主角", "守墓人", "平局")
    assert "对抗骰" in d["content"]


def test_group_check_all_present_auto_roll(db_factory, monkeypatch):
    """char=在场：公共/被动感知 → 在场每个玩家角色各自自动掷（不挂 pending）。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    chunks = _run(
        db, module, hero, teammates, session,
        "一声闷响从墙内传来。\n[DICE_CHECK: skill=聆听, char=在场]", monkeypatch,
    )
    dice = _dice(chunks)
    actors = sorted(d["metadata"]["actor"] for d in dice)
    assert actors == ["主角", "阿尔法"]                 # 在场两人都掷了
    assert _of_type(chunks, "check_request") == []      # 群检不挂 pending


def test_group_check_named_list(db_factory, monkeypatch):
    """chars=名单：仅名单内成员各自检定。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    chunks = _run(
        db, module, hero, teammates, session,
        "[DICE_CHECK: skill=侦查, chars=主角]", monkeypatch,
    )
    dice = _dice(chunks)
    assert [d["metadata"]["actor"] for d in dice] == ["主角"]


def test_group_check_scene_filtered(db_factory, monkeypatch):
    """char=在场 只覆盖与主角同场景者：分头在别处的队友不参与本地声响检定。"""
    db = db_factory()
    module, hero, teammates, session = _seed(db)
    # 主角在 hall，队友阿尔法分头去了 study
    session.current_scene_id = "hall"
    session.world_state = {"party_locations": {hero.id: "hall", teammates[0].id: "study"}}
    db.add(session)
    db.commit()
    chunks = _run(
        db, module, hero, teammates, session,
        "[DICE_CHECK: skill=聆听, char=在场]", monkeypatch,
    )
    actors = [d["metadata"]["actor"] for d in _dice(chunks)]
    assert actors == ["主角"]        # 只有同场景的主角检定，别处的阿尔法不掷
