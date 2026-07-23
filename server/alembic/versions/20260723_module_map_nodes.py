"""沙盘统一地图节点：场景节点与普通节点共享坐标/地貌数据。"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7b3c9d1e520"
down_revision: Union[str, Sequence[str], None] = "e8b2c6d4f091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 支持版本号被回退但 schema 已包含新列的修复场景，避免重复加列。
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("modules")}
    if "map_nodes" not in columns:
        op.add_column(
            "modules",
            sa.Column("map_nodes", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("modules")}
    if "map_nodes" in columns:
        op.drop_column("modules", "map_nodes")
