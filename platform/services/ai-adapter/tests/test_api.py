from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_is_mock_and_keyless():
    response = client.get("/ai/health", headers={"x-request-id": "health-test"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "health-test"
    assert response.json() == {"status": "ok", "provider": "mock", "llm_required": False}


def test_query_rewrite_is_deterministic_and_extracts_price():
    payload = {
        "query": "Laptop under $500",
        "locale": "en-US",
        "filters": {"brand": "Demo"},
        "request_id": "rewrite-1",
    }
    first = client.post("/ai/query-rewrite", json=payload)
    second = client.post("/ai/query-rewrite", json=payload)
    assert first.status_code == 200
    assert first.json()["provider"] == "mock"
    assert first.json()["rewritten_query"] == "laptop notebook computer"
    assert first.json()["extracted_filters"] == {"brand": "Demo", "price_lte": 500.0}
    for field in ("rewritten_query", "extracted_filters", "confidence", "explanation", "provider"):
        assert first.json()[field] == second.json()[field]


def test_rerank_uses_lexical_overlap():
    response = client.post(
        "/ai/rerank",
        json={
            "query": "red running shoes",
            "request_id": "rerank-1",
            "candidates": [
                {"product_id": "b", "title": "Coffee mug", "bm25_score": 2},
                {"product_id": "a", "title": "Red running athletic shoes", "bm25_score": 2},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["ranked_product_ids"] == ["a", "b"]
    assert response.json()["provider"] == "mock"


def test_strategy_suggestion_requires_approval():
    response = client.post(
        "/ai/strategy-suggest",
        json={
            "query_metrics": [{"query": "wireless lamp", "zero_result": True}],
            "current_strategy": {},
            "evidence": [],
            "request_id": "strategy-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["requires_approval"] is True
    assert body["proposed_changes"][0]["path"] == "/rewrite_rules/0"


def test_validation_rejects_unknown_fields():
    response = client.post(
        "/ai/query-rewrite",
        json={"query": "tv", "locale": "en-US", "filters": {}, "request_id": "x", "bad": 1},
    )
    assert response.status_code == 422

