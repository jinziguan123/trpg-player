"""规则书 RAG 入库与检索的单元测试。

不下载真实嵌入模型：用确定性 FakeEmbedder（按字符散列成词袋向量），
余弦相似度即反映字符重叠，足以验证「切块→嵌入→存储→检索」链路与降级。
"""

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.embedding import Embedder
from app.models import Base, RuleChunk, Rulebook  # noqa: F401 注册表
from app.services import rulebook_service


class FakeEmbedder(Embedder):
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


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_chunk_pages_tracks_page_and_filters_short():
    pages = [
        (1, "甲" * 600),          # 长页 → 切多块
        (2, "乙乙"),               # 太短 → 丢弃
        (3, "丙" * 100),          # 一块
    ]
    chunks = rulebook_service.chunk_pages(pages, size=500, overlap=80)
    pages_seen = {c["page"] for c in chunks}
    assert pages_seen == {1, 3}              # 第 2 页被过滤
    assert all(len(c["text"]) >= rulebook_service.MIN_CHUNK_CHARS for c in chunks)
    # ordinal 全书连续递增
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
    # 第 1 页 600 字、步长 420 → 2 块
    assert sum(1 for c in chunks if c["page"] == 1) == 2


def test_ingest_and_retrieve_roundtrip(db_factory, monkeypatch):
    db = db_factory()
    canned = [
        (1, "技能检定流程：投掷 d100，结果小于等于技能值即为普通成功，小于等于一半为困难成功，难度由守秘人根据情境选定。"),
        (2, "孤注一掷：当一次技能检定失败后，玩家可以加倍投入资源重新掷骰一次；若再次失败，后果将显著加重，由守秘人裁定。"),
        (3, "理智检定：当调查员遭遇超自然或恐怖事物时进行理智检定，失败会损失理智值，理智值归零将陷入永久性的疯狂状态。"),
    ]
    monkeypatch.setattr(rulebook_service, "extract_pages", lambda b: canned)

    book = Rulebook(title="测试规则书", rule_system="coc", status="indexing")
    db.add(book)
    db.commit()

    fake = FakeEmbedder()
    rulebook_service.ingest_rulebook(db, book, b"%PDF-fake", embedder=fake)

    assert book.status == "ready"
    assert book.chunk_count == 3
    assert book.embed_model == "fake-test"
    assert db.query(RuleChunk).count() == 3

    hits = rulebook_service.retrieve(
        db, "孤注一掷 重掷", "coc", k=2, embedder=fake,
    )
    assert hits, "应检索到结果"
    assert "孤注一掷" in hits[0]["text"]      # 最相关的是孤注一掷那块
    assert hits[0]["page"] == 2
    assert hits[0]["score"] >= hits[-1]["score"]  # 降序


def test_retrieve_empty_when_no_rulebook(db_factory):
    db = db_factory()
    assert rulebook_service.retrieve(db, "任何问题", "coc", embedder=FakeEmbedder()) == []


def test_retrieve_filters_by_rule_system(db_factory, monkeypatch):
    db = db_factory()
    monkeypatch.setattr(
        rulebook_service, "extract_pages",
        lambda b: [(1, "这是龙与地下城的专属规则内容，篇幅足够长以通过最小切块的过滤阈值，确保能够正常完成入库流程。")],
    )
    book = Rulebook(title="DnD书", rule_system="dnd", status="indexing")
    db.add(book)
    db.commit()
    rulebook_service.ingest_rulebook(db, book, b"x", embedder=FakeEmbedder())
    # 按 coc 检索拿不到 dnd 的内容
    assert rulebook_service.retrieve(db, "规则", "coc", embedder=FakeEmbedder()) == []
    assert rulebook_service.retrieve(db, "规则", "dnd", embedder=FakeEmbedder())


def test_delete_rulebook_removes_chunks(db_factory, monkeypatch):
    db = db_factory()
    monkeypatch.setattr(
        rulebook_service, "extract_pages",
        lambda b: [(1, "需要被删除的规则书内容，长度足够通过最小切块阈值的限制要求，确保能够正常完成整个入库流程。")],
    )
    book = Rulebook(title="待删", rule_system="coc", status="indexing")
    db.add(book)
    db.commit()
    rulebook_service.ingest_rulebook(db, book, b"x", embedder=FakeEmbedder())
    assert db.query(RuleChunk).count() == 1

    assert rulebook_service.delete_rulebook(db, book.id) is True
    assert db.query(RuleChunk).count() == 0
    assert db.get(Rulebook, book.id) is None
    assert rulebook_service.delete_rulebook(db, "不存在") is False
