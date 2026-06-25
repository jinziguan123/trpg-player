from sqlalchemy import JSON, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class GameSession(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "game_sessions"

    module_id: Mapped[str] = mapped_column(ForeignKey("modules.id"))
    status: Mapped[str] = mapped_column(
        Enum("setup", "active", "paused", "ended", name="session_status"),
        default="setup",
    )
    player_character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True
    )
    current_scene_id: Mapped[str | None] = mapped_column(nullable=True)
    world_state: Mapped[dict] = mapped_column(JSON, default=dict)
    turn_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
