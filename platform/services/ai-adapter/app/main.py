from __future__ import annotations

import json
import logging
import os
import time
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .models import (
    QueryRewriteRequest,
    QueryRewriteResponse,
    RerankRequest,
    RerankResponse,
    StrategySuggestRequest,
    StrategySuggestResponse,
)
from .provider import load_provider

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
logger = logging.getLogger("ai-adapter")
provider = load_provider()

REQUESTS = Counter("ai_adapter_requests_total", "AI adapter calls", ["path", "status"])
LATENCY = Histogram("ai_adapter_latency_seconds", "AI adapter latency", ["path"])

app = FastAPI(
    title="Commerce SearchOps AI Adapter",
    version="1.0.0",
    description="Stable optional AI boundary with a deterministic no-key mock provider.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        REQUESTS.labels(request.url.path, str(status)).inc()
        LATENCY.labels(request.url.path).observe(elapsed / 1000)
        logger.info(
            json.dumps(
                {
                    "event": "request.complete",
                    "service": "ai-adapter",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "latency_ms": elapsed,
                }
            )
        )


@app.get("/ai/health")
def health() -> dict:
    return {"status": "ok", "provider": provider.name, "llm_required": False}


@app.get("/ready")
def ready() -> dict:
    return {"status": "ready", "provider": provider.name}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# 三条 AI 路由都带 response_model_exclude_none=True。
#
# 唯一可能为 None 的字段就是新增的 model (其余字段全部必填), 所以它的作用面精确等于一件事:
# provider 没有声明模型标识时, 让 "model" 这个键整体消失, 而不是序列化成 "model": null。
#
# 为什么要这样: 一是与 Java 侧 spring.jackson.default-property-inclusion=non_null 的行为对齐,
# 两侧对"没有值"的线格式表示因此完全一致; 二是既有响应的键集合逐字节不变, 按精确键集断言的
# 既有契约测试 (tests/test_providers_echo.py 的七字段断言) 不需要改一个字符就仍然通过。
@app.post("/ai/query-rewrite", response_model=QueryRewriteResponse,
          response_model_exclude_none=True)
def query_rewrite(payload: QueryRewriteRequest) -> QueryRewriteResponse:
    started = time.perf_counter()
    rewritten, filters, confidence, explanation = provider.rewrite(payload)
    return QueryRewriteResponse(
        original_query=payload.query,
        rewritten_query=rewritten,
        extracted_filters=filters,
        confidence=confidence,
        explanation=explanation,
        provider=provider.name,
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        # provider.name 只说得出"哪个 provider 类跑的", provider.model 才说得出"哪个模型跑的"。
        # 见 app.provider.Provider.model: 既有第三方 provider 继承默认 None, 键随之消失。
        model=provider.model_for("rewrite"),
    )


@app.post("/ai/rerank", response_model=RerankResponse, response_model_exclude_none=True)
def rerank(payload: RerankRequest) -> RerankResponse:
    started = time.perf_counter()
    ids, scores, explanation = provider.rerank(payload)
    return RerankResponse(
        ranked_product_ids=ids,
        scores=scores,
        explanation=explanation,
        provider=provider.name,
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        model=provider.model_for("rerank"),
    )


@app.post("/ai/strategy-suggest", response_model=StrategySuggestResponse,
          response_model_exclude_none=True)
def strategy_suggest(payload: StrategySuggestRequest) -> StrategySuggestResponse:
    started = time.perf_counter()
    changes, impact, refs, confidence, risk = provider.suggest(payload)
    return StrategySuggestResponse(
        proposed_changes=changes,
        expected_impact=impact,
        evidence_refs=refs,
        confidence=confidence,
        risk_level=risk,
        requires_approval=True,
        provider=provider.name,
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        model=provider.model_for("suggest"),
    )

