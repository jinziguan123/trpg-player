from sqlalchemy import JSON, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Asset(Base, UUIDMixin, TimestampMixin):
    """地图素材库的一件素材：一张独立 PNG + 元数据。

    地图按「类型默认素材 / 显式 asset_id」引用这些素材渲染——加素材只需往库里加一条，
    不改渲染代码或坐标，彻底解决可扩充性。kind 用普通字符串（不枚举），便于将来扩类型。
    """

    __tablename__ = "assets"

    name: Mapped[str] = mapped_column(default="")
    # 类型：floor/wall/door/water/rubble/furniture/item/npc/enemy/player/feature… 可扩
    kind: Mapped[str] = mapped_column(index=True, default="furniture")
    filename: Mapped[str] = mapped_column()  # 磁盘文件名（assets_dir 下）
    mime: Mapped[str] = mapped_column(default="image/png")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(Text, default="")
    license: Mapped[str] = mapped_column(default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
