"""Handouts（手书：信件/报纸/日记/便条）一等公民的回归测试（不依赖真实 LLM）。

覆盖：[HANDOUT] 指令解析与落库、幂等（重复发放只一次）、未知 id 静默跳过、
发放后进线索台账（status=known, kind=handout）、KP 上下文含可发放清单且不含正文、
无 handouts 的旧模组行为不变、解析 prompt 与落库路径。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 — 注册建表
from app.models.module import Module
from app.models.session import GameSession
from app.models.session_participant import SessionParticipant  # noqa: F401 — 注册建表
from app.services import chat_service, module_service, session_service, world_memory


HANDOUT_CONTENT = "亲爱的玛丽：\n若你读到这封信，我恐怕已不在人世。\n——你的父亲"

HANDOUTS = [
    {
        "id": "handout_letter",
        "title": "父亲的遗书",
        "kind": "letter",
        "content": HANDOUT_CONTENT,
        "location": "scene_1",
        "trigger_condition": "搜查书房抽屉",
    },
    {
        "id": "handout_news",
        "title": "阿卡姆广告报头版",
        "kind": "news",
        "content": "本埠惊现离奇失踪案……",
        "location": "scene_1",
        "trigger_condition": "阅读报架上的报纸",
    },
]


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, handouts=None):
    module = Module(
        title="测试模组", rule_system="coc",
        npcs=[], scenes=[], clues=[],
        handouts=handouts if handouts is not None else list(HANDOUTS),
    )
    char = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, char])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=char.id, status="active",
    )
    db.add(session)
    db.commit()
    session_service.add_event(
        db, session.id, "action", "我搜查书房的抽屉", actor_id=char.id, actor_name=char.name,
    )
    return module, char, session


async def _collect(agen):
    return [c async for c in agen]


def _handout_events(db, session_id):
    return [
        e for e in session_service.get_session_events(db, session_id)
        if e.event_type == "system" and (e.metadata_ or {}).get("kind") == "handout"
    ]


def _run_commands(db, session, module, char, text):
    return asyncio.run(_collect(
        chat_service._process_commands(db, session.id, text, module, char, session, None)
    ))


# ---------- 指令解析与落库 ----------

def test_handout_command_persists_original_content(db_factory):
    db = db_factory()
    module, char, session = _seed(db)

    chunks = _run_commands(
        db, session, module, char, "你在抽屉深处摸到一封信。\n[HANDOUT: id=handout_letter]",
    )

    evs = _handout_events(db, session.id)
    assert len(evs) == 1
    ev = evs[0]
    assert ev.content == HANDOUT_CONTENT  # 原文一字不改
    meta = ev.metadata_ or {}
    assert meta["kind"] == "handout"
    assert meta["handout_id"] == "handout_letter"
    assert meta["title"] == "父亲的遗书"
    assert meta["handout_kind"] == "letter"
    # 正常广播给玩家（handout 是给玩家看的）
    assert any("handout_letter" in c for c in chunks)


def test_handout_command_tolerates_bare_id(db_factory):
    """容忍漏写 id=（与 SET_FLAG 同款宽容）。"""
    db = db_factory()
    module, char, session = _seed(db)

    _run_commands(db, session, module, char, "[HANDOUT: handout_news]")

    evs = _handout_events(db, session.id)
    assert len(evs) == 1
    assert (evs[0].metadata_ or {})["handout_id"] == "handout_news"


def test_handout_tag_is_command_not_leaked(db_factory):
    """HANDOUT 在指令前缀表内：流式过滤会把标签当指令剔除，不泄漏进旁白。"""
    assert chat_service._is_cmd_tag("HANDOUT: id=handout_letter")
    assert "HANDOUT:" in chat_service.CMD_TAG_PREFIXES


# ---------- 幂等与未知 id ----------

def test_handout_reissue_is_idempotent(db_factory):
    db = db_factory()
    module, char, session = _seed(db)

    _run_commands(db, session, module, char, "[HANDOUT: id=handout_letter]")
    # 同一 id 再发（同轮双写 + 再来一轮），都只落库一次
    _run_commands(
        db, session, module, char,
        "[HANDOUT: id=handout_letter]\n[HANDOUT: id=handout_letter]",
    )

    assert len(_handout_events(db, session.id)) == 1


def test_handout_unknown_id_silently_skipped(db_factory):
    db = db_factory()
    module, char, session = _seed(db)

    chunks = _run_commands(db, session, module, char, "[HANDOUT: id=no_such_handout]")

    assert _handout_events(db, session.id) == []
    assert not any('"kind": "handout"' in c for c in chunks)


def test_handout_noop_for_module_without_handouts(db_factory):
    """无 handouts 的旧模组：指令静默跳过，不炸也不落库。"""
    db = db_factory()
    module, char, session = _seed(db, handouts=[])

    _run_commands(db, session, module, char, "[HANDOUT: id=handout_letter]")

    assert _handout_events(db, session.id) == []


# ---------- 发放进台账 ----------

def test_handout_issue_recorded_in_ledger(db_factory):
    db = db_factory()
    module, char, session = _seed(db)

    _run_commands(db, session, module, char, "[HANDOUT: id=handout_letter]")
    db.refresh(session)

    ws = session.world_state or {}
    assert "handout_letter" in (ws.get("handouts_issued") or [])
    entry = (ws.get("clue_ledger") or {}).get("handout_letter") or {}
    assert entry.get("status") == "known"
    assert entry.get("kind") == "handout"
    assert char.id in (entry.get("discovered_by") or [])


def test_record_handout_issue_pure_and_idempotent():
    ws = {"clue_ledger": {"clue_x": {"status": "partial"}}}
    out = world_memory.record_handout_issue(ws, "h1", "遗书", ["char_a"], 7)
    # 纯函数：不改入参
    assert "handouts_issued" not in ws
    assert world_memory.handout_issued(out, "h1")
    assert not world_memory.handout_issued(ws, "h1")
    assert out["clue_ledger"]["h1"]["status"] == "known"
    assert out["clue_ledger"]["h1"]["kind"] == "handout"
    assert out["clue_ledger"]["clue_x"] == {"status": "partial"}  # 既有线索不受影响
    # 重复发放 no-op（列表不重复膨胀）
    again = world_memory.record_handout_issue(out, "h1", "遗书", ["char_b"], 9)
    assert again["handouts_issued"].count("h1") == 1


# ---------- KP 上下文 ----------

def test_kp_context_lists_handouts_without_content(db_factory):
    db = db_factory()
    module, char, session = _seed(db)
    events = session_service.get_session_events(db, session.id)

    system = ctx.build_kp_context(session, module, char, events)[0]["content"]

    assert "[HANDOUT" in system
    assert "handout_letter" in system and "父亲的遗书" in system
    assert "搜查书房抽屉" in system  # 发放条件在清单里
    assert HANDOUT_CONTENT.split("\n")[1] not in system  # 正文绝不进清单
    assert "发过的绝不重复发" in system


def test_kp_context_excludes_issued_handouts(db_factory):
    db = db_factory()
    module, char, session = _seed(db)
    _run_commands(db, session, module, char, "[HANDOUT: id=handout_letter]")
    db.refresh(session)
    events = session_service.get_session_events(db, session.id)

    system = ctx.build_kp_context(session, module, char, events)[0]["content"]

    # 已发放的从「可发放清单」消失（清单行格式 id｜类型｜标题｜条件），未发放的仍在；
    # 已发放的经线索台账自然呈现
    assert "handout_letter｜信件" not in system
    assert "handout_news｜报纸" in system
    assert "handout_letter" in system  # 台账里可见


def test_kp_context_no_handout_section_when_module_has_none(db_factory):
    """无 handouts 的旧模组：上下文与本特性引入前完全一致（不含手书小节）。"""
    db = db_factory()
    module, char, session = _seed(db, handouts=[])
    events = session_service.get_session_events(db, session.id)

    system = ctx.build_kp_context(session, module, char, events)[0]["content"]

    assert "[HANDOUT" not in system
    assert "手书发放" not in system


def test_kp_context_no_handout_section_at_opening(db_factory):
    """开场（无事件）不广告发放能力，与其他运行时指令一致。"""
    db = db_factory()
    module, char, session = _seed(db)

    system = ctx.build_kp_context(session, module, char, [])[0]["content"]

    assert "[HANDOUT" not in system


def test_kp_context_section_gone_when_all_issued(db_factory):
    db = db_factory()
    module, char, session = _seed(db)
    _run_commands(
        db, session, module, char,
        "[HANDOUT: id=handout_letter]\n[HANDOUT: id=handout_news]",
    )
    db.refresh(session)
    events = session_service.get_session_events(db, session.id)

    system = ctx.build_kp_context(session, module, char, events)[0]["content"]

    assert "手书发放" not in system  # 全发完 → 小节不再注入（台账仍呈现已发放）


# ---------- 解析提取与落库路径 ----------

def test_parse_prompt_asks_for_verbatim_handouts():
    tpl = module_service.PARSE_PROMPT_TEMPLATE
    assert '"handouts"' in tpl
    assert "保留模组原文" in tpl
    assert "letter" in tpl and "news" in tpl and "diary" in tpl and "note" in tpl


def test_create_module_persists_handouts(db_factory):
    db = db_factory()
    module = module_service.create_module(
        db, {"title": "带手书的模组", "rule_system": "coc", "handouts": list(HANDOUTS)},
    )
    assert module.handouts == HANDOUTS


def test_create_module_defaults_handouts_empty(db_factory):
    db = db_factory()
    module = module_service.create_module(db, {"title": "旧式模组", "rule_system": "coc"})
    assert module.handouts == []


def test_update_module_replaces_handouts(db_factory):
    db = db_factory()
    module = module_service.create_module(
        db, {"title": "带手书的模组", "rule_system": "coc", "handouts": list(HANDOUTS)},
    )
    updated = module_service.update_module(
        db, module.id, {"title": "带手书的模组", "handouts": [HANDOUTS[0]]},
    )
    assert updated.handouts == [HANDOUTS[0]]
