"""assets

地图素材库：用户可上传的独立素材（一件一张图 + 元数据），地图按 id/类型引用渲染。

Revision ID: a1f4c7e2b905
Revises: d7a3f9c2e810
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f4c7e2b905"
down_revision: Union[str, Sequence[str], None] = "d7a3f9c2e810"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("license", sa.String(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assets_kind", "assets", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_assets_kind", table_name="assets")
    op.drop_table("assets")
