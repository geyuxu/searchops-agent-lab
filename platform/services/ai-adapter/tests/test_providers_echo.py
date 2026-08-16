"""Tests for the echo-upper verification provider.

The assertions mirror what the Java search service does with the response, so a green run here
means the search service will report ``ai_status=APPLIED`` / ``ai_applied=true`` rather than
silently falling back to BM25.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import (
    Candidate,
    QueryRewriteRequest,
    QueryRewriteResponse,
    RerankRequest,
    StrategySuggestRequest,
)
from app.provider import MockProvider, Provider, load_provider
from app.providers.echo_upper import EchoUpperProvider

ROOT = Path(__file__).resolve().parents[3]
PRODUCTS = ROOT / "data" / "processed" / "products.jsonl"

# The exact string an operator puts in AI_PROVIDER. WORKDIR is /app in the image and only the
# app package is copied in, so this dotted path is valid both in the container and locally.
DOTTED_PATH = "app.providers.echo_upper:EchoUpperProvider"

client = TestClient(main.app)


def normalize_like_java(value: str) -> str:
    """Reproduce SearchQueryCompiler.applyRewrite: trim, collapse whitespace, lowercase."""
    return " ".join(value.split()).lower()


def rewrite_request(query: str, **kwargs) -> QueryRewriteRequest:
    return QueryRewriteRequest(query=query, request_id="echo-test", **kwargs)


# Every query the request schema accepts, including the awkward ones: mixed case, padded and
# doubled whitespace, a query that already contains the marker, and whitespace only (which
# passes min_length=1).
QUERIES = [
    "laptop",
    "Red Running Shoes",
    "  usb-c   cable  ",
    "product",
    "PRODUCT product",
    " ",
    "tv under $500",
]


def test_ai_provider_dotted_path_loads_this_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extension point, exercised through the exact env var value operators will set."""
    monkeypatch.setenv("AI_PROVIDER", DOTTED_PATH)
    provider = load_provider()
    assert isinstance(provider, EchoUpperProvider)
    assert isinstance(provider, Provider)
    # Deliberately not "mock": the old client hardcoded 'mock'.equals(provider) and rejected
    # everything else, so a name collision here would hide the very defect being tested.
    assert provider.name == "echo-upper"
    assert provider.name != MockProvider.name


def test_constructor_takes_no_arguments() -> None:
    """load_provider() calls provider_class() with no arguments at app import time, and a
    constructor that raises leaves the container unhealthy — which wedges every service that
    declares depends_on: service_healthy on it."""
    assert list(inspect.signature(EchoUpperProvider.__init__).parameters) == ["self"]


@pytest.mark.parametrize("query", QUERIES)
def test_rewrite_is_deterministic(query: str) -> None:
    first = EchoUpperProvider().rewrite(rewrite_request(query))
    second = EchoUpperProvider().rewrite(rewrite_request(query))
    assert first == second


@pytest.mark.parametrize("query", QUERIES)
def test_rewrite_differs_from_original_after_java_normalization(query: str) -> None:
    """The exact condition AiAdapterClient uses to decide ai_applied / APPLIED vs NO_CHANGE."""
    rewritten, _, _, _ = EchoUpperProvider().rewrite(rewrite_request(query))
    assert normalize_like_java(rewritten) != normalize_like_java(query)


@pytest.mark.parametrize("query", QUERIES)
def test_rewritten_query_is_plain_ascii_and_never_blank(query: str) -> None:
    """A blank rewritten_query makes the search service report INVALID_RESPONSE; non-ASCII
    would not retrieve anything from the US-English ESCI subset."""
    rewritten, _, _, _ = EchoUpperProvider().rewrite(rewrite_request(query))
    assert rewritten.strip()
    assert rewritten.isascii()
    assert rewritten == rewritten.strip()
    assert rewritten.split()[-1] == EchoUpperProvider.MARKER


def test_rewrite_appends_marker_to_normalized_query() -> None:
    rewritten, _, confidence, explanation = EchoUpperProvider().rewrite(
        rewrite_request("  Wireless   NOISE cancelling Headphones ")
    )
    assert rewritten == "wireless noise cancelling headphones product"
    assert confidence == 1.0
    assert EchoUpperProvider.MARKER in explanation


def test_rewrite_passes_filters_through_without_mutating_the_request() -> None:
    payload = rewrite_request("laptop", filters={"brand": "Demo", "price_max": 500})
    _, extracted, _, _ = EchoUpperProvider().rewrite(payload)
    assert extracted == {"brand": "Demo", "price_max": 500}
    extracted["injected"] = True
    assert payload.filters == {"brand": "Demo", "price_max": 500}


def test_rerank_delegates_to_the_mock_provider() -> None:
    payload = RerankRequest(
        query="red running shoes",
        request_id="rerank-echo",
        candidates=[
            Candidate(product_id="b", title="Coffee mug", bm25_score=2),
            Candidate(product_id="a", title="Red running athletic shoes", bm25_score=2),
        ],
    )
    assert EchoUpperProvider().rerank(payload) == MockProvider().rerank(payload)


def test_suggest_delegates_to_the_mock_provider() -> None:
    payload = StrategySuggestRequest(
        query_metrics=[{"query": "wireless lamp", "zero_result": True}],
        request_id="strategy-echo",
    )
    assert EchoUpperProvider().suggest(payload) == MockProvider().suggest(payload)


def test_http_response_is_contract_complete_and_reports_the_provider_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the Java client actually parses off the wire."""
    monkeypatch.setattr(main, "provider", EchoUpperProvider())
    response = client.post(
        "/ai/query-rewrite",
        json={
            "query": "Laptop Bag",
            "locale": "en-US",
            "filters": {"brand": "Demo"},
            "request_id": "rewrite-echo-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    QueryRewriteResponse.model_validate(body)
    assert set(body) == {
        "original_query",
        "rewritten_query",
        "extracted_filters",
        "confidence",
        "explanation",
        "provider",
        "latency_ms",
    }
    assert body["provider"] == "echo-upper"
    assert body["original_query"] == "Laptop Bag"
    assert body["rewritten_query"] == "laptop bag product"
    assert normalize_like_java(body["rewritten_query"]) != normalize_like_java("Laptop Bag")
    assert body["extracted_filters"] == {"brand": "Demo"}
    assert body["latency_ms"] >= 0


def test_http_response_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "provider", EchoUpperProvider())
    payload = {"query": "usb c cable", "locale": "en-US", "filters": {}, "request_id": "echo-2"}
    first = client.post("/ai/query-rewrite", json=payload).json()
    second = client.post("/ai/query-rewrite", json=payload).json()
    for field in ("rewritten_query", "extracted_filters", "confidence", "explanation", "provider"):
        assert first[field] == second[field]


def test_health_reports_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "provider", EchoUpperProvider())
    assert client.get("/ai/health").json() == {
        "status": "ok",
        "provider": "echo-upper",
        "llm_required": False,
    }


def test_default_provider_is_untouched_by_this_module() -> None:
    """Guard the regression line: nothing here may leave app.main pointing at echo-upper."""
    assert main.provider.name == "mock"


@pytest.mark.skipif(not PRODUCTS.exists(), reason="data/processed is generated by `make data`")
def test_marker_token_is_present_in_the_indexed_corpus() -> None:
    """A marker absent from the index would contribute nothing to an `operator: or` BM25 query,
    so the hit list would be identical to the baseline and the rewrite would prove nothing."""
    token = re.compile(rf"\b{re.escape(EchoUpperProvider.MARKER)}\b")
    scanned = 0
    hits = 0
    with PRODUCTS.open() as handle:
        for line in handle:
            if scanned >= 2000:
                break
            scanned += 1
            document = json.loads(line)
            text = " ".join(
                str(document.get(field, ""))
                for field in ("title", "brand", "bullet_point", "description", "category")
            ).lower()
            if token.search(text):
                hits += 1
    assert scanned == 2000
    assert hits >= 50, f"marker matched only {hits}/{scanned} sampled products"
