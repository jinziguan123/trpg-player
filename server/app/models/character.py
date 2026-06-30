from typing import Optional

from sqlalchemy import JSON, Enum, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Character(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "characters"

    name: Mapped[str] = mapped_column()
    module_id: Mapped[Optional[str]] = mapped_column(ForeignKey("modules.id"), nullable=True)
    rule_system: Mapped[str] = mapped_column(Enum("coc", "dnd", name="rule_system"))
    is_player: Mapped[bool] = mapped_column(default=True)
    # 角色归属于某玩家 token（阶段 2 联机：带角色入场认领席位）；AI 角色为 None
    owner_token: Mapped[Optional[str]] = mapped_column(nullable=True, index=True)
    base_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    skills: Mapped[dict] = mapped_column(JSON, default=dict)
    system_data: Mapped[dict] = mapped_column(JSON, default=dict)
    backstory: Mapped[str] = mapped_column(Text, default="")
    # 角色状态（应用层概念，取值见 app/rules/coc/status.py）：正常/重伤/昏迷/死亡/
    # 临时疯狂/不定期疯狂/永久疯狂。用普通字符串，避免 DB 枚举 CHECK 约束阻挡新增状态。
    status: Mapped[str] = mapped_column(Text, default="active")
