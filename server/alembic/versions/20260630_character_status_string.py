"""character status enum -> string（放开角色状态取值，支持重伤/昏迷/疯狂等）

Revision ID: f1c7a3d9e004
Revises: e2d9a4f1c833
Create Date: 2026-06-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c7a3d9e004"
down_revision: Union[str, Sequence[str], None] = "e2d9a4f1c833"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 枚举(VARCHAR+CHECK) -> 普通字符串；batch 重建表即丢弃旧 CHECK 约束
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum("active", "dead", "incapacitated", name="character_status"),
            type_=sa.Text(),
            existing_nullable=False,
        )
    # 旧值 incapacitated 迁移为 major_wound（重伤）
    op.execute("UPDATE characters SET status='major_wound' WHERE status='incapacitated'")


def downgrade() -> None:
    op.execute("UPDATE characters SET status='incapacitated' WHERE status IN ('major_wound','unconscious')")
    op.execute(
        "UPDATE characters SET status='active' "
        "WHERE status IN ('temporary_insanity','indefinite_insanity','permanent_insanity')"
    )
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Text(),
            type_=sa.Enum("active", "dead", "incapacitated", name="character_status"),
            existing_nullable=False,
        )
