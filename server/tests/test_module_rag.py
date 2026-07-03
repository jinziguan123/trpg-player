"""模组原文 RAG 的单元与集成回归（不依赖真实 LLM / 嵌入模型）。

覆盖：切块与 scene_hint 回填纯函数、当前场景加权检索（固定向量桩）、
未建索引时 KP 上下文不含摘录小节、[MODULE_LOOKUP] 与 [RULE_LOOKUP] 合并配额。
"""

import asyncio

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.ai.embedding import Embedder
from app.models.base import Base
from app.models.character import Character
from app.models.event_log import EventLog  # noqa: F401 注册建表
from app.models.module import Module, ModuleChunk
from app.models.session import GameSession
from app.services import chat_service, module_rag_service, session_service


class FakeEmbedder(Embedder):
    """字符散列词袋向量：余弦相似度即反映字符重叠，足以驱动检索链路。"""

    model_name = "fake-test"
    dim = 64

    def _vec(self, t: str):
        v = np.zeros(self.dim, dtype=np.float32)
        for ch in t:
            v[ord(ch) % self.dim] += 1.0
        return v.tolist()

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FixedQueryEmbedder(Embedder):
    """查询侧返回固定向量：用于精确控制各块的余弦得分，验证加权。"""

    model_name = "fixed-test"
    dim = 4

    def __init__(self, query_vec):
        self._q = query_vec

    def embed_passages(self, texts):
        raise AssertionError("检索测试不应触发 passage 嵌入")

    def embed_query(self, text):
        return self._q


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


# ---------- 切块 ----------

def test_chunk_text_sliding_window_with_overlap():
    text = "甲" * 1200
    chunks = module_rag_service.chunk_text(text, size=500, overlap=50)
    # 步长 450：0-500 / 450-950 / 900-1200
    assert [c["ordinal"] for c in chunks] == [0, 1, 2]
    assert len(chunks[0]["text"]) == 500
    assert len(chunks[2]["text"]) == 300
    # 相邻块有 10% 重叠
    assert chunks[0]["text"][-50:] == chunks[1]["text"][:50]


def test_chunk_text_filters_short_and_empty():
    assert module_rag_service.chunk_text("") == []
    assert module_rag_service.chunk_text("太短") == []
    # 压掉连续空行后照常切块
    chunks = module_rag_service.chunk_text("第一段。\n\n\n" + "乙" * 100)
    assert len(chunks) == 1


# ---------- scene_hint 回填（纯函数） ----------

def test_backfill_scene_hints_matches_and_carries_forward():
    scenes = [
        {"id": "scene_1", "title": "第一章：旅馆大堂"},
        {"id": "scene_2", "name": "阴冷的地下室"},
    ]
    chunks = [
        {"ordinal": 0, "text": "序言与作者的话，不属于任何场景。"},
        {"ordinal": 1, "text": "第一章：旅馆大堂\n你们推门而入，霉味扑面。"},
        {"ordinal": 2, "text": "大堂的正文还在继续，柜台后无人应答。"},
        {"ordinal": 3, "text": "顺着楼梯向下，便是阴冷的地下室，水声隐约。"},
    ]
    out = module_rag_service.backfill_scene_hints(chunks, scenes)

    assert [c["scene_hint"] for c in out] == [None, "scene_1", "scene_1", "scene_2"]
    # 纯函数：不改动入参
    assert all("scene_hint" not in c for c in chunks)


def test_backfill_scene_hints_fuzzy_and_latest_title_wins():
    scenes = [
        {"id": "s1", "title": "旅馆 大堂"},   # 标题含空白 → 归一化后仍可匹配
        {"id": "s2", "title": "地下室"},
    ]
    chunks = [
        # 同一块内先后出现两个标题：后出现者统辖其后的正文
        {"ordinal": 0, "text": "你们离开旅馆大堂……推开暗门，《地下室》一片漆黑。"},
        {"ordinal": 1, "text": "黑暗中传来水声。"},
    ]
    out = module_rag_service.backfill_scene_hints(chunks, scenes)
    assert [c["scene_hint"] for c in out] == ["s2", "s2"]


def test_backfill_scene_hints_no_scenes():
    chunks = [{"ordinal": 0, "text": "任意文本"}]
    out = module_rag_service.backfill_scene_hints(chunks, None)
    assert out[0]["scene_hint"] is None


# ---------- 入库（状态机 + fail-open） ----------

def _make_module(db, raw="", scenes=None):
    module = Module(
        title="测试模组", rule_system="coc",
        raw_content=raw, scenes=scenes or [], npcs=[], clues=[],
    )
    db.add(module)
    db.commit()
    return module


def test_ingest_module_roundtrip(db_factory):
    db = db_factory()
    raw = "第一章：旅馆大堂\n" + "大堂里霉味扑面，柜台后无人应答。" * 30
    module = _make_module(db, raw=raw, scenes=[{"id": "scene_1", "title": "第一章：旅馆大堂"}])

    module_rag_service.ingest_module(db, module, embedder=FakeEmbedder())

    assert module.rag_status == "ready"
    rows = db.query(ModuleChunk).filter(ModuleChunk.module_id == module.id).all()
    assert rows
    assert all(r.scene_hint == "scene_1" for r in rows)


def test_ingest_module_fail_open(db_factory):
    db = db_factory()

    # 无原文 → failed（而非抛异常阻塞主流程）
    empty = _make_module(db, raw="")
    module_rag_service.ingest_module(db, empty, embedder=FakeEmbedder())
    assert empty.rag_status == "failed"

    # 嵌入报错 → failed，同样不上抛
    class BoomEmbedder(FakeEmbedder):
        def embed_passages(self, texts):
            raise RuntimeError("boom")

    broken = _make_module(db, raw="足够长的模组原文内容，" * 20)
    module_rag_service.ingest_module(db, broken, embedder=BoomEmbedder())
    assert broken.rag_status == "failed"
    assert db.query(ModuleChunk).filter(ModuleChunk.module_id == broken.id).count() == 0


# ---------- 检索加权（固定向量桩） ----------

def _add_chunk(db, module_id, ordinal, text, vec, scene_hint=None):
    db.add(ModuleChunk(
        module_id=module_id, scene_hint=scene_hint, ordinal=ordinal,
        text=text, embedding=np.asarray(vec, dtype=np.float32).tobytes(),
    ))


def test_retrieve_scene_boost_promotes_current_scene(db_factory):
    db = db_factory()
    module = _make_module(db, raw="x")
    # A：与 query 余弦 0.8，属于当前场景；B：余弦 1.0，属于其他场景
    _add_chunk(db, module.id, 0, "当前场景的原文", [0.8, 0.6, 0.0, 0.0], scene_hint="cur")
    _add_chunk(db, module.id, 1, "其他场景的原文", [1.0, 0.0, 0.0, 0.0], scene_hint="other")
    db.commit()

    emb = FixedQueryEmbedder([1.0, 0.0, 0.0, 0.0])

    # 不带场景：纯余弦，B 在前
    plain = module_rag_service.retrieve(db, module.id, "查询", k=2, embedder=emb)
    assert plain[0]["text"] == "其他场景的原文"

    # 带当前场景：A 得分 0.8×1.3=1.04 > 1.0，排位提升到首位
    boosted = module_rag_service.retrieve(
        db, module.id, "查询", k=2, scene_id="cur", embedder=emb,
    )
    assert boosted[0]["text"] == "当前场景的原文"
    assert boosted[0]["score"] == pytest.approx(0.8 * module_rag_service.SCENE_BOOST, abs=1e-4)


def test_retrieve_empty_when_no_chunks(db_factory):
    db = db_factory()
    module = _make_module(db, raw="x")
    assert module_rag_service.retrieve(
        db, module.id, "任何问题", embedder=FakeEmbedder(),
    ) == []


# ---------- KP 上下文：摘录小节与能力广告 ----------

def _seed_session(db, rag_status=""):
    module = Module(
        title="测试模组", rule_system="coc", npcs=[], clues=[],
        scenes=[{"id": "scene_1", "title": "旅馆大堂"}],
        rag_status=rag_status,
    )
    char = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, char])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=char.id, status="active",
        current_scene_id="scene_1",
    )
    db.add(session)
    db.commit()
    session_service.add_event(
        db, session.id, "action", "我尝试搬开石板", actor_id=char.id, actor_name=char.name,
    )
    return module, char, session


def test_kp_context_without_excerpts_has_no_section(db_factory):
    db = db_factory()
    module, char, session = _seed_session(db)
    events = session_service.get_session_events(db, session.id)

    system = ctx.build_kp_context(session, module, char, events)[0]["content"]
    assert "模组原文摘录" not in system
    assert "[MODULE_LOOKUP" not in system


def test_kp_context_with_excerpts_injects_section_with_warning(db_factory):
    db = db_factory()
    module, char, session = _seed_session(db)
    events = session_service.get_session_events(db, session.id)

    # 超过单块上限 → 应被截断到 MODULE_EXCERPT_MAX_CHARS 字（不写死字数，随常量走）
    long_text = "原" * (ctx.MODULE_EXCERPT_MAX_CHARS + 200)
    system = ctx.build_kp_context(
        session, module, char, events,
        module_excerpts=[{"text": long_text}, {"text": "短摘录"}],
        module_lookup_enabled=True,
    )[0]["content"]

    assert "模组原文摘录" in system
    # 泄密警示措辞（设计稿 4.2 的硬性要求）
    assert "泄密约束照常适用" in system
    assert "玩家尚未触及" in system
    # 单块截断 400 字
    assert long_text not in system
    assert long_text[:ctx.MODULE_EXCERPT_MAX_CHARS] + "…" in system
    assert "短摘录" in system
    # 已建索引 → 广告 [MODULE_LOOKUP]
    assert "[MODULE_LOOKUP" in system


def test_excerpts_helper_gates_on_rag_status(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed_session(db, rag_status="indexing")
    events = session_service.get_session_events(db, session.id)

    called = {"n": 0}

    def fake_retrieve(*a, **k):
        called["n"] += 1
        return [{"text": "命中片段", "scene_hint": "scene_1", "score": 0.9, "ordinal": 0}]

    monkeypatch.setattr(module_rag_service, "retrieve", fake_retrieve)

    # 未就绪（indexing/failed/空）→ None，且根本不发起检索
    assert chat_service._module_excerpts_for_context(
        db, module, session, events, {char.id},
    ) is None
    assert called["n"] == 0

    # 就绪 → 检索并返回片段；query 含场景标题与玩家最新输入
    module.rag_status = "ready"

    captured = {}

    def fake_retrieve2(db_, module_id, query, k=3, scene_id=None):
        captured["query"] = query
        captured["scene_id"] = scene_id
        return [{"text": "命中片段", "scene_hint": "scene_1", "score": 0.9, "ordinal": 0}]

    monkeypatch.setattr(module_rag_service, "retrieve", fake_retrieve2)
    hits = chat_service._module_excerpts_for_context(
        db, module, session, events, {char.id},
    )
    assert hits and hits[0]["text"] == "命中片段"
    assert "旅馆大堂" in captured["query"] and "搬开石板" in captured["query"]
    assert captured["scene_id"] == "scene_1"

    # 检索抛错 → fail-open 返回 None
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(module_rag_service, "retrieve", boom)
    assert chat_service._module_excerpts_for_context(
        db, module, session, events, {char.id},
    ) is None


# ---------- [MODULE_LOOKUP]：路由、配额合并、检索回灌 ----------

async def _collect(agen):
    return [c async for c in agen]


def test_process_commands_routes_module_lookup(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed_session(db, rag_status="ready")

    seen = {}

    async def fake_handle(db_, session_id, query, *a, **k):
        seen["query"] = query
        yield chat_service._make_chunk("system", "stub")

    monkeypatch.setattr(chat_service, "_handle_module_lookup", fake_handle)

    # MODULE_LOOKUP 为终止性指令：短路其后的 DICE_CHECK
    text = (
        "让我确认一下原文。\n"
        "[MODULE_LOOKUP: query=旅馆大堂的描写]\n"
        "[DICE_CHECK: skill=侦查, difficulty=normal]"
    )
    asyncio.run(_collect(
        chat_service._process_commands(db, session.id, text, module, char, session, None)
    ))

    assert seen["query"] == "旅馆大堂的描写"
    dice_events = [
        e for e in session_service.get_session_events(db, session.id)
        if e.event_type == "dice"
    ]
    assert dice_events == []


def test_lookup_quota_shared_between_rule_and_module(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed_session(db, rag_status="ready")

    calls = {"rule": 0, "module": 0}

    async def fake_rule(*a, **k):
        calls["rule"] += 1
        yield chat_service._make_chunk("system", "rule")

    async def fake_module(*a, **k):
        calls["module"] += 1
        yield chat_service._make_chunk("system", "module")

    monkeypatch.setattr(chat_service, "_handle_rule_lookup", fake_rule)
    monkeypatch.setattr(chat_service, "_handle_module_lookup", fake_module)

    def run(text, depth):
        asyncio.run(_collect(chat_service._process_commands(
            db, session.id, text, module, char, session, None, lookup_depth=depth,
        )))

    # 同一配额未用尽：两种查阅都可路由
    run("[MODULE_LOOKUP: query=原文]", chat_service.MAX_RULE_LOOKUPS - 1)
    assert calls["module"] == 1

    # 配额用尽（不论此前是哪种查阅消耗的）：两种查阅一并停用
    run("[RULE_LOOKUP: query=规则]", chat_service.MAX_RULE_LOOKUPS)
    run("[MODULE_LOOKUP: query=原文]", chat_service.MAX_RULE_LOOKUPS)
    assert calls["rule"] == 0
    assert calls["module"] == 1

    # allow_rule_lookup=False（续写阶段）同样对两者生效
    asyncio.run(_collect(chat_service._process_commands(
        db, session.id, "[MODULE_LOOKUP: query=原文]", module, char, session, None,
        allow_rule_lookup=False,
    )))
    assert calls["module"] == 1


def test_handle_module_lookup_retrieves_continues_and_persists(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed_session(db, rag_status="ready")

    captured = {}

    def fake_retrieve(db_, module_id, query, k=3, scene_id=None):
        captured["query"] = query
        captured["scene_id"] = scene_id
        return [{"text": "大堂吊灯上蒙着厚灰。", "scene_hint": "scene_1", "score": 0.9, "ordinal": 0}]

    monkeypatch.setattr(module_rag_service, "retrieve", fake_retrieve)

    async def fake_stream(kp, messages, result, npcs=None):
        captured["messages"] = messages
        result[0] = "吊灯的灰在你们头顶簌簌落下。"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    chunks = asyncio.run(_collect(
        chat_service._handle_module_lookup(
            db, session.id, "旅馆大堂", module, char, session, None,
        )
    ))

    assert any("翻阅模组手稿" in c for c in chunks)  # 透明提示
    assert captured["query"] == "旅馆大堂"
    assert captured["scene_id"] == "scene_1"  # 带当前场景做加权

    # 原文片段与泄密警示回灌进了续写上下文
    last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]["content"]
    assert "大堂吊灯上蒙着厚灰" in last_user
    assert "泄密约束照常适用" in last_user

    # 续写叙事落库
    narrs = [
        e for e in session_service.get_session_events(db, session.id)
        if e.event_type == "narration"
    ]
    assert any("簌簌落下" in e.content for e in narrs)


def test_handle_module_lookup_fallback_when_no_hits(db_factory, monkeypatch):
    db = db_factory()
    module, char, session = _seed_session(db, rag_status="ready")

    monkeypatch.setattr(module_rag_service, "retrieve", lambda *a, **k: [])

    captured = {}

    async def fake_stream(kp, messages, result, npcs=None):
        captured["messages"] = messages
        result[0] = "按既有资料续写。"
        result[1] = result[0]
        yield chat_service._make_chunk("narration", result[0], actor_name="KP")

    monkeypatch.setattr(chat_service, "_stream_narration_filtered", fake_stream)

    asyncio.run(_collect(
        chat_service._handle_module_lookup(
            db, session.id, "不存在的内容", module, char, session, None,
        )
    ))

    last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]["content"]
    assert "未在模组原文中找到" in last_user
