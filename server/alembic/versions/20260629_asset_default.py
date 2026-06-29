"""asset is_default

素材按类型设默认：每个 kind 至多一个 is_default=True，地图按类型取默认素材。

Revision ID: c3b8e1a6d720
Revises: a1f4c7e2b905
Create Date: 2026-06-29 01:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3b8e1a6d720"
down_revision: Union[str, Sequence[str], None] = "a1f4c7e2b905"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("assets", "is_default")
