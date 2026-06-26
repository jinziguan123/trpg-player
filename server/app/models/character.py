from typing import Optional

from sqlalchemy import JSON, Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Character(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "characters"

    name: Mapped[str] = mapped_column()
    module_id: Mapped[Optional[str]] = mapped_column(ForeignKey("modules.id"), nullable=True)
    rule_system: Mapped[str] = mapped_column(Enum("coc", "dnd", name="rule_system"))
    is_player: Mapped[bool] = mapped_column(default=True)
    base_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    skills: Mapped[dict] = mapped_column(JSON, default=dict)
    system_data: Mapped[dict] = mapped_column(JSON, default=dict)
    backstory: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        Enum("active", "dead", "incapacitated", name="character_status"),
        default="active",
    )
