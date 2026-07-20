"""真人 KP 私有工作区。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1e8b4d762af"
down_revision: Union[str, Sequence[str], None] = "a7c4e9f2b631"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "game_sessions",
        sa.Column("kp_state", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("game_sessions", "kp_state")
