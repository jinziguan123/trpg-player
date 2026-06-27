"""规则书 RAG：rulebooks + rule_chunks 表

Revision ID: d7a3f9c2e810
Revises: c5e9a1b7d402
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3f9c2e810"
down_revision: Union[str, Sequence[str], None] = "c5e9a1b7d402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rulebooks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("rule_system", sa.String(), nullable=False, server_default="coc"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="indexing"),
        sa.Column("embed_model", sa.String(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        "rule_chunks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "rulebook_id",
            sa.String(),
            sa.ForeignKey("rulebooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rule_system", sa.String(), nullable=False, server_default="coc"),
        sa.Column("page", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_rule_chunks_rulebook_id", "rule_chunks", ["rulebook_id"])
    op.create_index("ix_rule_chunks_rule_system", "rule_chunks", ["rule_system"])
    op.create_index(
        "ix_rule_chunks_book_ord", "rule_chunks", ["rulebook_id", "ordinal"]
    )


def downgrade() -> None:
    op.drop_index("ix_rule_chunks_book_ord", table_name="rule_chunks")
    op.drop_index("ix_rule_chunks_rule_system", table_name="rule_chunks")
    op.drop_index("ix_rule_chunks_rulebook_id", table_name="rule_chunks")
    op.drop_table("rule_chunks")
    op.drop_table("rulebooks")
