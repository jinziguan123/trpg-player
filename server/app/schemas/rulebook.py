from datetime import datetime

from pydantic import BaseModel


class RulebookRead(BaseModel):
    id: str
    title: str
    rule_system: str
    page_count: int
    chunk_count: int
    status: str
    embed_model: str
    error: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class RuleHit(BaseModel):
    text: str
    page: int
    score: float
    rulebook_id: str


class RuleSearchResponse(BaseModel):
    query: str
    hits: list[RuleHit]
