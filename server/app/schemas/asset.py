from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, computed_field


class AssetUpdate(BaseModel):
    """编辑素材：改名/改类别/改标签（均可选）。"""
    name: str | None = None
    kind: str | None = None
    tags: list[str] | None = None


class CategoryRead(BaseModel):
    key: str
    label: str
    builtin: bool = False


class CategoryWrite(BaseModel):
    key: str
    label: str = ""


class CategoryLabel(BaseModel):
    label: str


class AssetRead(BaseModel):
    id: str
    name: str
    kind: str
    mime: str
    tags: list
    source: str
    license: str
    builtin: bool
    is_default: bool = False
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def image_url(self) -> str:
        """前端取图地址（独立 PNG），渲染/编辑直接用。"""
        return f"/api/assets/{self.id}/image"
