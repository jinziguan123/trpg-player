"""session_participants.ready

大厅准备态：每个真人席位是否已准备。AI 席/房主席默认就绪。

Revision ID: c5e9a1b7d402
Revises: b4d8c1e6f307
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e9a1b7d402"
down_revision: Union[str, Sequence[str], None] = "b4d8c1e6f307"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "session_participants",
        sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    # 存量（多为已开局会话）回填为已准备，避免被新门槛卡住
    op.execute("UPDATE session_participants SET ready = 1")


def downgrade() -> None:
    op.drop_column("session_participants", "ready")
