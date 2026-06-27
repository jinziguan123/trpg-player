from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.session_participant import SessionParticipant


class GameSession(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "game_sessions"

    module_id: Mapped[str] = mapped_column(ForeignKey("modules.id"))
    status: Mapped[str] = mapped_column(
        Enum("setup", "active", "paused", "ended", name="session_status"),
        default="setup",
    )
    # 主角快捷字段：与 session_participants 中 is_primary 的席位对齐，便于兼容旧代码与展示。
    player_character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True
    )
    current_scene_id: Mapped[str | None] = mapped_column(nullable=True)
    world_state: Mapped[dict] = mapped_column(JSON, default=dict)
    turn_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    participants: Mapped[list["SessionParticipant"]] = relationship(
        "SessionParticipant",
        cascade="all, delete-orphan",
        order_by="SessionParticipant.seat_order",
    )
