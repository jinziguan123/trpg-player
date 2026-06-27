"""event_type 增加 ooc

为 OOC（场外）消息新增事件类型；SQLite 上 Enum 以 CHECK 约束实现，需重建约束。

Revision ID: 9a2e7b3c5f21
Revises: 7c1f2a9b4d10
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a2e7b3c5f21"
down_revision: Union[str, Sequence[str], None] = "7c1f2a9b4d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = sa.Enum("dialogue", "action", "dice", "narration", "system", name="event_type")
_NEW = sa.Enum(
    "dialogue", "action", "dice", "narration", "system", "ooc", name="event_type"
)


def upgrade() -> None:
    with op.batch_alter_table("event_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type", existing_type=_OLD, type_=_NEW, existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("event_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type", existing_type=_NEW, type_=_OLD, existing_nullable=False
        )
