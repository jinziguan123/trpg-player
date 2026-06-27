from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Rulebook(Base, UUIDMixin, TimestampMixin):
    """已入库的规则书（一份 PDF = 一条记录）。"""

    __tablename__ = "rulebooks"

    title: Mapped[str] = mapped_column()
    rule_system: Mapped[str] = mapped_column(default="coc")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # indexing（建索引中）/ ready（可检索）/ failed（失败）
    status: Mapped[str] = mapped_column(default="indexing")
    embed_model: Mapped[str] = mapped_column(default="")
    error: Mapped[str] = mapped_column(Text, default="")


class RuleChunk(Base, UUIDMixin):
    """规则书切块 + 嵌入向量（float32 原始字节存 BLOB）。"""

    __tablename__ = "rule_chunks"

    rulebook_id: Mapped[str] = mapped_column(
        ForeignKey("rulebooks.id", ondelete="CASCADE"), index=True
    )
    # 冗余 rule_system 便于按规则系统直接过滤检索，省一次 join
    rule_system: Mapped[str] = mapped_column(default="coc", index=True)
    page: Mapped[int] = mapped_column(Integer, default=0)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)


Index("ix_rule_chunks_book_ord", RuleChunk.rulebook_id, RuleChunk.ordinal)
