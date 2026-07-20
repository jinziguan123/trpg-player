from __future__ import annotations

from pydantic import BaseModel, Field


class KpWorkspaceUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=20000)
    auto_ai_teammates: bool | None = None


class KpDraftRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class KpPlanRequest(BaseModel):
    focus: str = Field(default="", max_length=1000)


class KpImagePreviewRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1500)
    title: str = Field(default="KP 配图", max_length=100)


class KpImagePublishRequest(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    title: str = Field(default="KP 配图", max_length=100)
    suggestion_key: str = Field(default="", max_length=300)
