import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import QueryRewriteResponse, RerankResponse, StrategySuggestResponse

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "packages" / "api-contracts"
client = TestClient(app)


def test_checked_in_openapi_has_all_stable_operations():
    """Path-level agreement only. Field-level drift is ``tests/test_contract_drift.py``.

    The subset assertions below are one-directional on purpose: the app also serves ``/ready``,
    which is an operational probe and deliberately outside the stable contract. What was missing
    was the other direction -- a documented path with no route behind it -- and version alignment,
    without which the file can claim to describe a release the process is not running.
    """
    documented = json.loads((CONTRACT / "openapi" / "ai-adapter.openapi.json").read_text())
    generated = app.openapi()
    required = {"/ai/health", "/ai/query-rewrite", "/ai/rerank", "/ai/strategy-suggest"}
    assert required.issubset(documented["paths"])
    assert required.issubset(generated["paths"])
    assert documented["info"]["version"] == "1.0.0"
    assert set(documented["paths"]) <= set(generated["paths"]), (
        f"documented but not served: {sorted(set(documented['paths']) - set(generated['paths']))}"
    )
    assert documented["info"]["version"] == generated["info"]["version"]


def test_query_rewrite_example_round_trips_contract():
    request = json.loads((CONTRACT / "examples" / "query-rewrite.request.json").read_text())
    response = client.post("/ai/query-rewrite", json=request)
    assert response.status_code == 200
    QueryRewriteResponse.model_validate(response.json())
    assert response.json()["provider"] == "mock"


def test_rerank_example_round_trips_contract():
    request = json.loads((CONTRACT / "examples" / "rerank.request.json").read_text())
    response = client.post("/ai/rerank", json=request)
    assert response.status_code == 200
    RerankResponse.model_validate(response.json())
    assert response.json()["ranked_product_ids"][0] == "p2"


def test_strategy_example_round_trips_contract():
    request = json.loads((CONTRACT / "examples" / "strategy-suggest.request.json").read_text())
    response = client.post("/ai/strategy-suggest", json=request)
    assert response.status_code == 200
    value = StrategySuggestResponse.model_validate(response.json())
    assert value.requires_approval is True
    assert value.provider == "mock"

