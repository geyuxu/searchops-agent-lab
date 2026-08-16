"""The response must say which *model* produced it, not only which provider class ran.

Why this is a file of its own rather than a few extra lines in the provider test modules: the
claim under test is not about any one provider. It is a property of the adapter's contract —
every ``/ai/*`` response carries a model identity, that identity follows delegation, and a
provider that declares none produces no key at all rather than a null. Each provider test module
can only ever check its own corner of that.

What the gap actually was. ``provider`` is a class name: ``mock``, ``langchain-rerank``. An
evaluation artifact carrying only that can prove "a LangChain reranker ran" and nothing more.
Swap ``AI_MODEL`` and every artifact keeps exactly the same shape while the numbers underneath
change — so ``experiments/rerank-holdout-n50.json`` (NDCG@10 0.5926, Δ+0.1207) was only
attributable to a model because a human typed the model name into a hand-written ``ai`` block
next to the metrics. A hand-written note is a comment, not evidence: nothing makes it agree with
the process that actually ran.

Three distinctions this file keeps apart, because collapsing any two of them re-opens the gap:

* **a model ran** vs **no model ran** — ``deterministic-mock`` vs a vendor model name;
* **no model ran** vs **nobody recorded it** — ``deterministic-mock`` vs an absent key;
* **this provider's model** vs **the delegate's model** — a provider that hands a capability to
  MockProvider must hand over the identity with it, or the artifact claims an LLM produced a
  deterministic answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import (
    ProposedChange,
    QueryRewriteRequest,
    QueryRewriteResponse,
    RerankRequest,
    RerankResponse,
    RerankScore,
    StrategySuggestRequest,
    StrategySuggestResponse,
)
from app.provider import MockProvider, Provider

client = TestClient(main.app)

REWRITE_REQUEST = {
    "query": "laptop under $500",
    "locale": "en-US",
    "filters": {},
    "request_id": "model-identity-rewrite",
}

RERANK_REQUEST = {
    "query": "red running shoes",
    "request_id": "model-identity-rerank",
    "candidates": [
        {"product_id": "a", "title": "Red running athletic shoes", "bm25_score": 3},
        {"product_id": "b", "title": "Coffee mug", "bm25_score": 2},
    ],
}

SUGGEST_REQUEST = {
    "query_metrics": [{"query": "wireless lamp", "zero_result": True}],
    "current_strategy": {},
    "evidence": [],
    "request_id": "model-identity-suggest",
}

ENDPOINTS = [
    pytest.param("/ai/query-rewrite", REWRITE_REQUEST, "rewrite", id="query-rewrite"),
    pytest.param("/ai/rerank", RERANK_REQUEST, "rerank", id="rerank"),
    pytest.param("/ai/strategy-suggest", SUGGEST_REQUEST, "suggest", id="strategy-suggest"),
]


class SilentProvider(Provider):
    """A third-party provider written before this field existed: it declares no model.

    Stands in for every provider outside this repository. It must keep working untouched, and its
    responses must omit the key rather than assert a model identity nobody supplied.
    """

    name = "silent-third-party"

    def __init__(self) -> None:
        self._delegate = MockProvider()

    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]:
        return self._delegate.rewrite(payload)

    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]:
        return self._delegate.rerank(payload)

    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]:
        return self._delegate.suggest(payload)


# ---------------------------------------------------------------------------
# The default deployment: a model identity is always present, and it is not a model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "payload", "_capability"), ENDPOINTS)
def test_every_response_carries_the_model_identity(
    path: str, payload: dict[str, Any], _capability: str
) -> None:
    response = client.post(path, json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "deterministic-mock"
    # Two different questions, two different fields. If the model identity were allowed to repeat
    # the provider name, an artifact could never distinguish "the mock ran" from "some model ran
    # behind a provider class that happens to be named after it".
    assert body["provider"] == "mock"
    assert body["model"] != body["provider"]


def test_the_mock_declares_a_non_model_identity_rather_than_nothing() -> None:
    """``deterministic-mock`` is a deliberate value, not a placeholder that was never filled in.

    Leaving it None would make "this run had no model in it" indistinguishable from "this run had
    a model but nobody wrote it down" — and those two say opposite things about whether a number
    in ``experiments/`` can be reproduced.
    """
    provider = MockProvider()

    assert provider.model == "deterministic-mock"
    for capability in ("rewrite", "rerank", "suggest"):
        assert provider.model_for(capability) == "deterministic-mock"
    # Unmistakably not a vendor model name: no reader will mistake it for something that could be
    # requested from an API.
    assert "mock" in provider.model


# ---------------------------------------------------------------------------
# A provider that declares nothing: absent key, never a null and never a guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "payload", "_capability"), ENDPOINTS)
def test_a_provider_without_a_model_omits_the_key_entirely(
    monkeypatch: pytest.MonkeyPatch, path: str, payload: dict[str, Any], _capability: str
) -> None:
    """Absent, not ``null``.

    Mirrors the Java side's ``spring.jackson.default-property-inclusion=non_null``, so both ends
    express "no value" the same way, and — the reason it matters here — the existing key-set
    contract tests (``tests/test_providers_echo.py``) keep passing without being edited, which is
    the evidence that this addition did not change any existing response shape.
    """
    monkeypatch.setattr(main, "provider", SilentProvider())

    response = client.post(path, json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "model" not in body
    assert body["provider"] == "silent-third-party"


def test_an_older_provider_needs_no_changes_at_all() -> None:
    """The field is opt-in for provider authors: the base class supplies the whole default."""
    provider = SilentProvider()

    assert provider.model is None
    assert provider.model_for("rerank") is None
    # Nothing about the abstract contract changed, so a provider implemented against the old
    # signature is still a Provider.
    assert isinstance(provider, Provider)


# ---------------------------------------------------------------------------
# Delegation carries the identity with it
# ---------------------------------------------------------------------------


def test_identity_follows_delegation_rather_than_the_running_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability delegated to MockProvider reports MockProvider's identity.

    This is the failure this design exists to prevent: an adapter configured with a real model,
    asked for a capability it delegates, would otherwise stamp the real model's name onto a
    deterministic answer. The resulting artifact does not merely lack evidence — it carries
    false evidence, which no downstream check can detect.
    """

    class HalfRealProvider(SilentProvider):
        name = "half-real"
        model = "vendor-model-2026-01-01"

        def model_for(self, capability: str) -> str | None:
            return self.model if capability == "rerank" else self._delegate.model_for(capability)

    monkeypatch.setattr(main, "provider", HalfRealProvider())

    reranked = client.post("/ai/rerank", json=RERANK_REQUEST).json()
    rewritten = client.post("/ai/query-rewrite", json=REWRITE_REQUEST).json()

    assert reranked["model"] == "vendor-model-2026-01-01"
    assert rewritten["model"] == "deterministic-mock"
    # Same provider, same process, same request — only the capability differs.
    assert reranked["provider"] == rewritten["provider"] == "half-real"


# ---------------------------------------------------------------------------
# Backward compatibility of the schema itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_class", "body"),
    [
        pytest.param(
            QueryRewriteResponse,
            {
                "original_query": "tv",
                "rewritten_query": "tv television",
                "extracted_filters": {},
                "confidence": 0.5,
                "explanation": "",
                "provider": "mock",
                "latency_ms": 1,
            },
            id="query-rewrite",
        ),
        pytest.param(
            RerankResponse,
            {
                "ranked_product_ids": ["a"],
                "scores": [{"product_id": "a", "score": 1.0}],
                "explanation": "",
                "provider": "mock",
                "latency_ms": 1,
            },
            id="rerank",
        ),
        pytest.param(
            StrategySuggestResponse,
            {
                "proposed_changes": [],
                "expected_impact": "",
                "evidence_refs": [],
                "confidence": 0.5,
                "risk_level": "low",
                "requires_approval": True,
                "provider": "mock",
                "latency_ms": 1,
            },
            id="strategy-suggest",
        ),
    ],
)
def test_a_response_recorded_before_this_field_existed_still_validates(
    model_class: type, body: dict[str, Any]
) -> None:
    """The field is optional on the way in too.

    ``StrictModel`` is ``extra="forbid"``, so the field had to be declared; declaring it
    *required* would have broken every archived response and every existing contract test. This
    is the assertion that keeps it optional.
    """
    parsed = model_class.model_validate(body)

    assert parsed.model is None
    assert "model" not in parsed.model_dump(exclude_none=True)
