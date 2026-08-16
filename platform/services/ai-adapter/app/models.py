from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: 跑出这次结果的**模型**标识, 与 ``provider`` 并列但回答的是另一个问题。
#:
#: ``provider`` 只说得出"哪一个 provider 类跑的"(``mock`` / ``langchain-rerank`` / …), 一个
#: provider 类换个 ``AI_MODEL`` 就是完全不同的一次运行, 而指标里看不出任何区别。评测产物因此
#: 只能自证"用了 LangChain 重排", 不能自证"用了 qwen3.7-flash-2026-07-15 重排" —— 半年后想复现
#: 一个 +0.1349 的结论, 缺的正是这一半。
#:
#: 为什么必须**可选**(默认 None): ``StrictModel`` 是 extra="forbid", 新字段必须正式声明; 而
#: 声明成必填会让所有既有 provider 与既有契约测试当场挂掉。None 的语义是"这个 provider 没有
#: 声明模型标识", 而不是"跑的是某个未知模型" —— 服务端(``/ai/*`` 路由)以
#: ``response_model_exclude_none=True`` 序列化, 于是 None 时整个键消失, 既有响应形状逐字节不变。
#:
#: 为什么不叫 ``model_id``: pydantic v2 的保护命名空间是 ``model_`` 前缀, ``model_id`` 会触发
#: 保护命名空间告警; 裸 ``model`` 不在其列。
#:
#: 填值的来源是 ``app.provider.Provider.model_for(capability)`` 而不是一个扁平属性 —— 因为本仓库
#: 的 provider 会把自己不实现的能力逐字委托给 MockProvider, 那些响应必须回报委托方的标识, 否则
#: 产物会声称一次 mock 改写是某个真实模型写的。
ModelIdentity = str | None


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
    #: 见 ``ModelIdentity``。可选, 缺省时该键在响应里整体消失。
    model: ModelIdentity = None


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
    #: 见 ``ModelIdentity``。重排链路是这个字段最要紧的地方: 留出集上 +0.1349 的结论
    #: 完全由重排产生, 而产物里此前没有任何东西能说出那是哪个模型排的。
    model: ModelIdentity = None


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
    #: 见 ``ModelIdentity``。策略建议同样需要可追溯: 一条被批准的变更提案, 半年后必须还能
    #: 说出它是哪个模型提的。
    model: ModelIdentity = None

