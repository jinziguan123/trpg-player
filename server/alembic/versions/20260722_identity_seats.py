"""身份席位模型：独立房主与新房间单 token 单席位。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f7a9c2e1b3"
down_revision: Union[str, Sequence[str], None] = "c1e8b4d762af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "game_sessions",
        sa.Column("host_token", sa.String(), nullable=True),
    )
    op.create_index("ix_game_sessions_host_token", "game_sessions", ["host_token"])
    op.add_column(
        "game_sessions",
        sa.Column("identity_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "session_participants",
        sa.Column("identity_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "uq_session_participant_token_v2",
        "session_participants",
        ["session_id", "owner_token"],
        unique=True,
        sqlite_where=sa.text("identity_version >= 2 AND owner_token IS NOT NULL"),
        postgresql_where=sa.text("identity_version >= 2 AND owner_token IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_session_participant_token_v2", table_name="session_participants")
    op.drop_column("session_participants", "identity_version")
    op.drop_index("ix_game_sessions_host_token", table_name="game_sessions")
    op.drop_column("game_sessions", "identity_version")
    op.drop_column("game_sessions", "host_token")
