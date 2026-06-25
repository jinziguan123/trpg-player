from sqlalchemy import JSON, Enum, Text
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
