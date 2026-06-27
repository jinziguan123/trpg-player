from sqlalchemy import Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class SessionParticipant(Base, UUIDMixin, TimestampMixin):
    """一个会话中的一个席位。

    多人 roadmap 阶段 1：会话由单席位（GameSession.player_character_id）升级为
    1:N 多席位，本表是参与关系的事实来源。``role`` 区分真人 / AI 队友，
    ``is_primary`` 标记主角（与 GameSession.player_character_id 对齐，作兼容快捷字段）。
    """

    __tablename__ = "session_participants"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("game_sessions.id"), index=True
    )
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id"), index=True)
    role: Mapped[str] = mapped_column(
        Enum("human", "ai", name="participant_role"), default="human"
    )
    seat_order: Mapped[int] = mapped_column(default=0)
    is_primary: Mapped[bool] = mapped_column(default=False)
