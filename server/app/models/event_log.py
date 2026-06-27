from datetime import datetime

from sqlalchemy import JSON, Enum, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDMixin


class EventLog(Base, UUIDMixin):
    __tablename__ = "event_logs"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("game_sessions.id"), index=True
    )
    sequence_num: Mapped[int] = mapped_column()
    event_type: Mapped[str] = mapped_column(
        Enum(
            "dialogue", "action", "dice", "narration", "system", "ooc",
            name="event_type",
        )
    )
    actor_id: Mapped[str | None] = mapped_column(nullable=True)
    actor_name: Mapped[str] = mapped_column(default="")
    content: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[list] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
