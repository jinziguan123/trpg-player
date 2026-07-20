"""修复已标记身份迁移但缺少列的数据库。

早期开发库曾在 d4f7a9c2e1b3 已应用后修改该迁移，造成版本号已前进、
game_sessions.identity_version 却不存在。本迁移按实际 schema 幂等补齐，
同时覆盖非事务 DDL 中断后可能遗留的其它身份字段和索引。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b2c6d4f091"
down_revision: Union[str, Sequence[str], None] = "d4f7a9c2e1b3"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    session_columns = _columns("game_sessions")
    if "host_token" not in session_columns:
        op.add_column(
            "game_sessions",
            sa.Column("host_token", sa.String(), nullable=True),
        )
    if "identity_version" not in session_columns:
        op.add_column(
            "game_sessions",
            sa.Column(
                "identity_version", sa.Integer(), nullable=False, server_default="1",
            ),
        )
    if "ix_game_sessions_host_token" not in _indexes("game_sessions"):
        op.create_index(
            "ix_game_sessions_host_token", "game_sessions", ["host_token"],
        )

    participant_columns = _columns("session_participants")
    if "identity_version" not in participant_columns:
        op.add_column(
            "session_participants",
            sa.Column(
                "identity_version", sa.Integer(), nullable=False, server_default="1",
            ),
        )
    if "uq_session_participant_token_v2" not in _indexes("session_participants"):
        op.create_index(
            "uq_session_participant_token_v2",
            "session_participants",
            ["session_id", "owner_token"],
            unique=True,
            sqlite_where=sa.text(
                "identity_version >= 2 AND owner_token IS NOT NULL"
            ),
            postgresql_where=sa.text(
                "identity_version >= 2 AND owner_token IS NOT NULL"
            ),
        )


def downgrade() -> None:
    # 这是 schema 漂移修复，不能可靠区分字段来自原迁移还是本迁移；回退版本号即可。
    pass
