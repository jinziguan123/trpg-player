"""模组原文 RAG：module_chunks 表 + modules.rag_status 列

Revision ID: b6e2d9a4c517
Revises: f1c7a3d9e004
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2d9a4c517"
down_revision: Union[str, Sequence[str], None] = "f1c7a3d9e004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 存量模组默认空字符串=未建索引（通过「重建原文索引」按钮补建）
    op.add_column(
        "modules",
        sa.Column("rag_status", sa.String(), nullable=False, server_default=""),
    )
    op.create_table(
        "module_chunks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "module_id",
            sa.String(),
            sa.ForeignKey("modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scene_hint", sa.String(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_module_chunks_module_id", "module_chunks", ["module_id"])
    op.create_index(
        "ix_module_chunks_module_ord", "module_chunks", ["module_id", "ordinal"]
    )


def downgrade() -> None:
    op.drop_index("ix_module_chunks_module_ord", table_name="module_chunks")
    op.drop_index("ix_module_chunks_module_id", table_name="module_chunks")
    op.drop_table("module_chunks")
    op.drop_column("modules", "rag_status")
