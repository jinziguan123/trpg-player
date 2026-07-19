from sqlalchemy import JSON, Enum, ForeignKey, Index, Integer, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Module(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "modules"

    title: Mapped[str] = mapped_column()
    rule_system: Mapped[str] = mapped_column(Enum("coc", "dnd", name="rule_system"))
    description: Mapped[str] = mapped_column(Text, default="")
    theme: Mapped[str] = mapped_column(default="default")
    world_setting: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_content: Mapped[str] = mapped_column(Text, default="")
    scenes: Mapped[list] = mapped_column(JSON, default=list)
    npcs: Mapped[list] = mapped_column(JSON, default=list)
    maps: Mapped[list] = mapped_column(JSON, default=list)
    clues: Mapped[list] = mapped_column(JSON, default=list)
    triggers: Mapped[list] = mapped_column(JSON, default=list)
    # 手书（Handouts）：模组原文里的信件/报纸/日记/便条等一等公民实体，
    # 形如 [{id, title, kind(letter|news|diary|note), content(原文), location, trigger_condition}]
    handouts: Mapped[list] = mapped_column(JSON, default=list)
    # 幕后真相（守秘人资讯）：整个事件的来龙去脉/真凶/时间线，KP 专属参考，玩家永不可见。
    # 注入 KP/planner/幕后推演上下文；空串 = 模组无此段或旧模组未重导。
    truth: Mapped[str] = mapped_column(Text, default="")
    # 原文 RAG 索引状态：""=未建（存量模组）/ indexing / ready / failed，与规则书状态机同形
    rag_status: Mapped[str] = mapped_column(default="")


class ModuleChunk(Base, UUIDMixin):
    """模组原文切块 + 嵌入向量（float32 原始字节存 BLOB），镜像 RuleChunk 形态。"""

    __tablename__ = "module_chunks"

    module_id: Mapped[str] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE"), index=True
    )
    # 章节归属的场景 id（切块后按场景标题在块内模糊匹配回填），检索时用于当前场景加权
    scene_hint: Mapped[str | None] = mapped_column(nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)


Index("ix_module_chunks_module_ord", ModuleChunk.module_id, ModuleChunk.ordinal)
