"""真人 KP 模式与 KP 席位。"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c4e9f2b631"
down_revision: Union[str, Sequence[str], None] = "f4c2d8a91e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE participant_role ADD VALUE IF NOT EXISTS 'kp'")
    op.add_column(
        "game_sessions",
        sa.Column(
            "kp_mode",
            sa.Enum("ai", "human", name="kp_mode"),
            nullable=False,
            server_default="ai",
        ),
    )


def downgrade() -> None:
    op.drop_column("game_sessions", "kp_mode")
