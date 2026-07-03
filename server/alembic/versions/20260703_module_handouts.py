"""Handouts 一等公民：modules 表加 handouts JSON 列

Revision ID: c8f4a1b7d920
Revises: b6e2d9a4c517
Create Date: 2026-07-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8f4a1b7d920"
down_revision: Union[str, Sequence[str], None] = "b6e2d9a4c517"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 存量模组默认空列表 = 无手书（行为与本特性引入前完全一致）
    op.add_column(
        "modules",
        sa.Column("handouts", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("modules", "handouts")
