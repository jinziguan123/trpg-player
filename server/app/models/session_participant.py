from sqlalchemy import Enum, ForeignKey, Index, text
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
    # 阶段 2：human 空席可先建后认领，故 character_id 可空
    character_id: Mapped[str | None] = mapped_column(
        ForeignKey("characters.id"), index=True, nullable=True
    )
    role: Mapped[str] = mapped_column(
        Enum("human", "ai", "kp", name="participant_role"), default="human"
    )
    seat_order: Mapped[int] = mapped_column(default=0)
    is_primary: Mapped[bool] = mapped_column(default=False)
    # 认领该席位的玩家 token（AI 席与未认领的空 human 席为 None）
    owner_token: Mapped[str | None] = mapped_column(nullable=True, index=True)
    # human 席是否已被认领并填入角色
    claimed: Mapped[bool] = mapped_column(default=True)
    # 大厅准备态：AI 席与房主席默认 True；空/已认领的真人席需玩家手动准备
    ready: Mapped[bool] = mapped_column(default=False)
    # 新身份模型版本。旧房间保留 1，允许历史上 KP/玩家共用 token；新房间为 2，
    # 由数据库部分唯一索引保证一个 token 在一个房间内只能占一个席位。
    identity_version: Mapped[int] = mapped_column(default=1, server_default="1")

    __table_args__ = (
        Index(
            "uq_session_participant_token_v2",
            "session_id",
            "owner_token",
            unique=True,
            sqlite_where=text("identity_version >= 2 AND owner_token IS NOT NULL"),
            postgresql_where=text("identity_version >= 2 AND owner_token IS NOT NULL"),
        ),
    )
