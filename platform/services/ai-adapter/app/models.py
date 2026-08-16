from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRewriteRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    locale: str = Field(default="en-US", min_length=2, max_length=20)
    filters: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=1, max_length=128)


class QueryRewriteResponse(StrictModel):
    original_query: str
    rewritten_query: str
    extracted_filters: dict[str, Any]
    confidence: float = Field(ge=0, le=1)
    explanation: str
    provider: str
    latency_ms: int = Field(ge=0)


class Candidate(StrictModel):
    product_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=2000)
    brand: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=5000)
    bm25_score: float = 0


class RerankRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    candidates: list[Candidate] = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=128)


class RerankScore(StrictModel):
    product_id: str
    score: float


class RerankResponse(StrictModel):
    ranked_product_ids: list[str]
    scores: list[RerankScore]
    explanation: str
    provider: str
    latency_ms: int = Field(ge=0)


class StrategySuggestRequest(StrictModel):
    query_metrics: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    current_strategy: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any] | str] = Field(default_factory=list, max_length=500)
    request_id: str = Field(min_length=1, max_length=128)


class ProposedChange(StrictModel):
    operation: str
    path: str
    value: Any
    reason: str


class StrategySuggestResponse(StrictModel):
    proposed_changes: list[ProposedChange]
    expected_impact: str
    evidence_refs: list[str]
    confidence: float = Field(ge=0, le=1)
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool
    provider: str
    latency_ms: int = Field(ge=0)

