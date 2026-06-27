"""session_participants

为多人 roadmap 阶段 1 新增会话席位表，把单席位会话升级为 1:N 多席位。

Revision ID: 7c1f2a9b4d10
Revises: 53e23cb587bd
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7c1f2a9b4d10"
down_revision: Union[str, Sequence[str], None] = "53e23cb587bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_participants",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("human", "ai", name="participant_role"),
            nullable=False,
        ),
        sa.Column("seat_order", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["game_sessions.id"]),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_participants_session_id",
        "session_participants",
        ["session_id"],
    )
    op.create_index(
        "ix_session_participants_character_id",
        "session_participants",
        ["character_id"],
    )

    # 回填：把已有会话的 player_character_id 写成一个主角席位，保证旧存档可读。
    bind = op.get_bind()
    sessions = bind.execute(
        sa.text(
            "SELECT id, player_character_id, created_at FROM game_sessions "
            "WHERE player_character_id IS NOT NULL"
        )
    ).fetchall()
    for row in sessions:
        bind.execute(
            sa.text(
                "INSERT INTO session_participants "
                "(id, session_id, character_id, role, seat_order, is_primary, "
                "created_at, updated_at) VALUES "
                "(:id, :sid, :cid, 'human', 0, 1, :ts, :ts)"
            ),
            {
                "id": _gen_uuid(),
                "sid": row[0],
                "cid": row[1],
                "ts": row[2],
            },
        )


def downgrade() -> None:
    op.drop_index(
        "ix_session_participants_character_id", table_name="session_participants"
    )
    op.drop_index(
        "ix_session_participants_session_id", table_name="session_participants"
    )
    op.drop_table("session_participants")


def _gen_uuid() -> str:
    import uuid

    return str(uuid.uuid4())
