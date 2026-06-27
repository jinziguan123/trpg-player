"""阶段2 身份与席位：owner_token / room_code / 空席可认领

为联机加入：Character.owner_token、GameSession.room_code、
SessionParticipant.owner_token + claimed，且 character_id 改可空（空 human 席）。

Revision ID: b4d8c1e6f307
Revises: 9a2e7b3c5f21
Create Date: 2026-06-27 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d8c1e6f307"
down_revision: Union[str, Sequence[str], None] = "9a2e7b3c5f21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("characters", sa.Column("owner_token", sa.String(), nullable=True))
    op.create_index("ix_characters_owner_token", "characters", ["owner_token"])

    op.add_column("game_sessions", sa.Column("room_code", sa.String(), nullable=True))
    op.create_index("ix_game_sessions_room_code", "game_sessions", ["room_code"])

    with op.batch_alter_table("session_participants", schema=None) as batch_op:
        batch_op.alter_column(
            "character_id", existing_type=sa.String(), nullable=True
        )
        batch_op.add_column(sa.Column("owner_token", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("claimed", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    op.create_index(
        "ix_session_participants_owner_token", "session_participants", ["owner_token"]
    )

    # 回填：给已有会话生成房间码
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM game_sessions")).fetchall()
    for row in rows:
        bind.execute(
            sa.text("UPDATE game_sessions SET room_code = :code WHERE id = :id"),
            {"code": uuid.uuid4().hex[:6].upper(), "id": row[0]},
        )


def downgrade() -> None:
    op.drop_index("ix_session_participants_owner_token", table_name="session_participants")
    with op.batch_alter_table("session_participants", schema=None) as batch_op:
        batch_op.drop_column("claimed")
        batch_op.drop_column("owner_token")
        batch_op.alter_column(
            "character_id", existing_type=sa.String(), nullable=False
        )
    op.drop_index("ix_game_sessions_room_code", table_name="game_sessions")
    op.drop_column("game_sessions", "room_code")
    op.drop_index("ix_characters_owner_token", table_name="characters")
    op.drop_column("characters", "owner_token")
