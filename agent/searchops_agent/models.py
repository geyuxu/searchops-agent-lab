"""与 Java 服务 `lab.searchops.domain.ApiModels` 对齐的传输模型。

字段名与 `@JsonProperty` 保持一致；这里不做重命名，避免两侧契约漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    """搜索策略配置——也是进化/搜索类实验的参数空间。"""

    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    rewrite_rules: list[dict[str, Any]] = Field(default_factory=list)
    pinned_product_ids: list[str] = Field(default_factory=list)
    blocked_product_ids: list[str] = Field(default_factory=list)
    brand_boosts: dict[str, float] = Field(default_factory=dict)
    field_weights: dict[str, float] = Field(default_factory=dict)
    minimum_score: float = 0.0


class Strategy(BaseModel):
    id: str
    version: int
    name: str
    status: str
    config: StrategyConfig
    created_by: str | None = None
    created_at: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    published_at: str | None = None
    supersedes_version: int | None = None


class QueryMetric(BaseModel):
    """单查询指标——配对统计检验的最小单位。"""

    query_id: int
    query: str = ""
    precision10: float
    recall10: float
    mrr10: float
    ndcg10: float
    zero_result: bool


class EvaluationResult(BaseModel):
    run_id: str | None = None
    strategy_version: int
    query_count: int
    precision10: float
    recall10: float
    mrr10: float
    ndcg10: float
    zero_result_rate: float
    queries: list[QueryMetric] = Field(default_factory=list)
    generated_at: str | None = None


class PreviewResponse(BaseModel):
    current: dict[str, Any]
    proposed: dict[str, Any]
    changed_positions: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = True
