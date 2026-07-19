"""幕后真相一等公民：modules 表加 truth TEXT 列（守秘人资讯，KP 专属）

Revision ID: e3a9c6b2f471
Revises: c8f4a1b7d920
Create Date: 2026-07-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a9c6b2f471"
down_revision: Union[str, Sequence[str], None] = "c8f4a1b7d920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 存量模组默认空串 = 无结构化真相（行为与本特性引入前完全一致，KP 仍靠原文 RAG）
    op.add_column(
        "modules",
        sa.Column("truth", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("modules", "truth")
