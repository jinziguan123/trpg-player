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


def test_npc_dialogue_still_extracted():
    """模组 NPC 的引号台词仍正常抽取为对话。"""
    text = "托马斯·金博尔抬起头，露出微笑。“下午好，年轻人。”"
    npcs = [{"name": "托马斯·金博尔"}]
    result = ["", "", []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert "托马斯·金博尔" in speakers


def test_unnamed_npc_uses_role_label_not_named_npc():
    """无名 NPC（护工）的台词归到其身份，不被硬塞给在场的有名 NPC。"""
    npcs = [{"name": "史蒂芬·诺特"}]
    text = (
        "史蒂芬说道：“你们去疗养院吧。”\n\n"
        "一位路过的护工微笑致意：护工：“您是来找海恩斯院长的吧？请上二楼。”"
    )
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert [n for n, _ in result[2]] == ["史蒂芬·诺特", "护工"], result[2]


def test_sign_labels_not_extracted_as_dialogue():
    """门牌/招牌等带引号的标识文本，不被抽成『仅在别处被提及』的 NPC 的台词。"""
    npcs = [{"name": "维托里奥·马卡里奥"}]
    text = (
        "前租户——马卡里奥一家——卷入某种悲剧，夫妻二人双双精神失常。\n\n"
        "北墙上排列着四扇磨砂玻璃的门，上面分别贴着褪色的字牌——“恢复名誉”、“波士顿环球报社”、“中央图书馆”。"
    )
    result = ["", "", [], [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert result[2] == [], result[2]          # 没有任何标签被错抽成对话
    assert "恢复名誉" in result[0]              # 标识文本留在旁白


def test_written_text_stays_in_narration():
    """『写着：「…」』是书写内容而非台词，应留在旁白、不抽成对话气泡。"""
    npcs = [{"name": "史蒂芬·诺特"}]
    text = '史蒂芬说道：“看这个。”\n\n照片角落写着：“1899年，圣玛丽疗养院全体人员。”'
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    assert [n for n, _ in result[2]] == ["史蒂芬·诺特"], result[2]
    assert "1899年" in result[0]


def test_speaker_not_hijacked_by_mentioned_npc():
    """当前说话人的台词不被『仅在旁白里被提及』的其他 NPC（如历史人物）夺走。"""
    npcs = [{"name": "史蒂芬·诺特"}, {"name": "沃尔特·科比特"}]
    text = (
        "史蒂芬开口道：“先看看这些。”\n\n"
        "他从桌上拿起文件递过来。这是科比特的一些零星记录，年份不全。\n\n"
        "“档案馆四点关门，你们还有一个半小时。”\n\n"
        "“这是通行证，带着吧。”"
    )
    result = ["", "", [], []]
    asyncio.run(_collect(
        chat_service._stream_narration_filtered(_FakeKP(text), [], result, npcs=npcs)
    ))
    speakers = [name for name, _ in result[2]]
    assert speakers == ["史蒂芬·诺特", "史蒂芬·诺特", "史蒂芬·诺特"], speakers


def test_group_tags_split_scenes(db_factory):
    """分头行动：[GROUP] 标记把各组内容落库到对应 group，供前端分栏。"""
    npcs = [{"name": "护工"}]
    text = (
        "[GROUP: scene=档案馆]亨利在档案馆翻查旧卷宗，灰尘扑面。\n\n"
        "[GROUP: scene=疗养院]莫妮卡走进疗养院走廊，护工说道：“您找海恩斯院长？”"
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


def test_persisted_order_interleaves_narration_and_dialogue(db_factory):
    """落库要保留「旁白/对话交错」的原始顺序（与流式渲染一致），而非旁白全在前、对话全在后。"""
    npcs = [{"name": "史蒂芬·诺特"}]
    text = (
        "诺特先生走上前来。\n\n"
        "“万幸你们来了。”\n\n"
        "他压低声音，凑近了些。\n\n"
        "“房子就在彻斯特街。”\n\n"
        "他递来一把铜钥匙。"
    )
    result = ["", "", [], []]
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
