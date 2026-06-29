from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, computed_field


class AssetRead(BaseModel):
    id: str
    name: str
    kind: str
    mime: str
    tags: list
    source: str
    license: str
    builtin: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def image_url(self) -> str:
        """前端取图地址（独立 PNG），渲染/编辑直接用。"""
        return f"/api/assets/{self.id}/image"
