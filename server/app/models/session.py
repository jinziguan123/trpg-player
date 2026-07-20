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
    # KP 来源：ai 为兼容旧局的默认模式，human 表示由真人 KP 席位驱动。
    kp_mode: Mapped[str] = mapped_column(
        Enum("ai", "human", name="kp_mode"), default="ai", server_default="ai",
    )
    # 房间分享码（阶段 2 联机：他人凭码加入认领空席）；建房时生成、唯一
    room_code: Mapped[str | None] = mapped_column(nullable=True, index=True)
    # 主角快捷字段：与 session_participants 中 is_primary 的席位对齐，便于兼容旧代码与展示。
    player_character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id"), nullable=True
    )
    current_scene_id: Mapped[str | None] = mapped_column(nullable=True)
    world_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # 真人 KP 私有工作区：笔记、自动队友偏好等。绝不加入 SessionRead，避免玩家端读取。
    kp_state: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default="{}",
    )
    turn_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    participants: Mapped[list["SessionParticipant"]] = relationship(
        "SessionParticipant",
        cascade="all, delete-orphan",
        order_by="SessionParticipant.seat_order",
    )
