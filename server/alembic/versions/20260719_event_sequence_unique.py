"""事件序号按会话唯一

Revision ID: f4c2d8a91e60
Revises: e3a9c6b2f471
Create Date: 2026-07-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f4c2d8a91e60"
down_revision: Union[str, Sequence[str], None] = "e3a9c6b2f471"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT_NAME = "uq_event_logs_session_sequence"


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT session_id, sequence_num, COUNT(*) AS duplicate_count
            FROM event_logs
            GROUP BY session_id, sequence_num
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "event_logs 存在重复会话序号，未自动猜测重排："
            f"session_id={duplicate[0]}, sequence_num={duplicate[1]}, count={duplicate[2]}。"
            "请先人工审计事件顺序，再重试迁移。"
        )

    # SQLite 需要 batch_alter_table 重建表来添加唯一约束；其它数据库同样可安全执行。
    with op.batch_alter_table("event_logs", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            CONSTRAINT_NAME,
            ["session_id", "sequence_num"],
        )


def downgrade() -> None:
    with op.batch_alter_table("event_logs", schema=None) as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
