"""The checked-in ai-adapter contract must describe the adapter that is actually running.

Why a contract file needs a test at all. ``packages/api-contracts`` is what ``ai-handoff.md``
points at when it promises that swapping the model provider needs no change upstream. That promise
is only worth something if the file is true, and nothing about a JSON document keeps it true --
someone adds a field to ``app/models.py``, every test still passes, and the contract silently
starts describing a version of the adapter that no longer exists. The next person writes a client
from the contract and comes up short at runtime, which is the most expensive place to find out.

That is not hypothetical here: this suite already covered the ``model`` identity field three ways
(``tests/test_model_identity.py``) while ``ai-adapter.openapi.json`` mentioned it zero times.

What the existing check missed. ``test_checked_in_openapi_has_all_stable_operations`` asserts that
four paths are a subset of the documented paths. Paths are the coarsest thing in an OpenAPI
document; every schema underneath them could be wrong and that assertion stays green. This module
takes the check down to the field level, which is where drift actually happens.

Two directions, and both matter:

* **implementation field missing from the contract** -- a client written against the contract never
  learns the field exists. Sharper than usual here: every response schema is
  ``additionalProperties: false``, so an undocumented field does not merely go unmentioned, it
  makes a genuine adapter response *invalid* against its own contract.
* **contract field missing from the implementation** -- a client sends or expects something nothing
  will ever honour. A rename produces both halves at once.

So the assertion is set equality, not a subset in either direction.

The wire, not just the types. ``model_fields`` is what the adapter *can* express; the last test
posts to all three endpoints and checks the bytes that actually come back, which is the only way to
catch a route that serialises something its response model never declared.

Nothing here needs a network or a key: the default provider is the deterministic mock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import models
from app.main import app

PLATFORM = Path(__file__).resolve().parents[3]
CONTRACT = PLATFORM / "packages" / "api-contracts" / "openapi" / "ai-adapter.openapi.json"
DOCUMENT = json.loads(CONTRACT.read_text())
SCHEMAS = DOCUMENT["components"]["schemas"]

client = TestClient(app)

#: Every pydantic model that reaches the wire, keyed by the schema that is supposed to describe it.
#: Requests are in here too: a request field the contract omits is the same defect pointing the
#: other way, and the adapter is ``extra="forbid"``, so a *documented* field it does not implement
#: is a 422 for anyone who sends it.
REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "QueryRewriteRequest": models.QueryRewriteRequest,
    "Candidate": models.Candidate,
    "RerankRequest": models.RerankRequest,
    "StrategySuggestRequest": models.StrategySuggestRequest,
}

#: The three top-level responses plus the two objects nested inside them.
RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "QueryRewriteResponse": models.QueryRewriteResponse,
    "RerankResponse": models.RerankResponse,
    "StrategySuggestResponse": models.StrategySuggestResponse,
}

NESTED_MODELS: dict[str, type[BaseModel]] = {
    "RerankScore": models.RerankScore,
    "ProposedChange": models.ProposedChange,
}

WIRE_MODELS = {**REQUEST_MODELS, **NESTED_MODELS, **RESPONSE_MODELS}

ALL_CASES = [pytest.param(name, model, id=name) for name, model in WIRE_MODELS.items()]
RESPONSE_CASES = [pytest.param(name, model, id=name) for name, model in RESPONSE_MODELS.items()]

#: (path, payload, schema) for the three endpoints, so the live check covers all of them.
LIVE_ENDPOINTS = [
    pytest.param(
        "/ai/query-rewrite",
        {
            "query": "laptop under $500",
            "locale": "en-US",
            "filters": {},
            "request_id": "contract-drift-rewrite",
        },
        "QueryRewriteResponse",
        id="query-rewrite",
    ),
    pytest.param(
        "/ai/rerank",
        {
            "query": "red running shoes",
            "request_id": "contract-drift-rerank",
            "candidates": [
                {"product_id": "a", "title": "Red running athletic shoes", "bm25_score": 3},
                {"product_id": "b", "title": "Coffee mug", "bm25_score": 2},
            ],
        },
        "RerankResponse",
        id="rerank",
    ),
    pytest.param(
        "/ai/strategy-suggest",
        {
            "query_metrics": [{"query": "wireless lamp", "zero_result": True}],
            "current_strategy": {},
            "evidence": [],
            "request_id": "contract-drift-suggest",
        },
        "StrategySuggestResponse",
        id="strategy-suggest",
    ),
]


def required_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.is_required()}


def documented(name: str) -> dict[str, Any]:
    assert name in SCHEMAS, (
        f"{name} is implemented in app/models.py but has no schema in {CONTRACT.name}; "
        f"a client written from the contract cannot see it at all"
    )
    return SCHEMAS[name]


# ---------------------------------------------------------------------------
# Field-level agreement between app/models.py and the checked-in document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "model"), ALL_CASES)
def test_documented_properties_match_the_implementation_exactly(
    name: str, model: type[BaseModel]
) -> None:
    """The assertion the reviewer asked for, in both directions.

    Equality rather than a subset because each direction is a real defect with a different victim:
    an undocumented field strands the client author, a documented-but-absent field strands the
    running client. A rename is both at once, and only equality reports it as one change.
    """
    implemented = set(model.model_fields)
    described = set(documented(name).get("properties", {}))

    assert implemented == described, (
        f"{name} has drifted: "
        f"implemented but undocumented={sorted(implemented - described)}, "
        f"documented but not implemented={sorted(described - implemented)}. "
        f"Update {CONTRACT} alongside app/models.py."
    )


@pytest.mark.parametrize("name", list(RESPONSE_MODELS))
def test_response_schemas_forbid_undocumented_properties(name: str) -> None:
    """Why the test above has to be equality and not a friendly subset.

    With ``additionalProperties: false`` a field present on the wire but absent from the contract
    does not merely go undocumented: it makes an ordinary, correct adapter response fail validation
    against its own contract. Anyone validating responses in CI would see the adapter as broken.
    """
    assert documented(name).get("additionalProperties") is False


@pytest.mark.parametrize(("name", "model"), RESPONSE_CASES)
def test_response_required_sets_agree_exactly(name: str, model: type[BaseModel]) -> None:
    """Optionality is part of the contract, not a footnote.

    A field documented as required that the adapter may omit breaks strict clients on a perfectly
    normal response; a field documented as optional that is in fact always present makes every
    consumer write a null branch it will never take.
    """
    assert required_fields(model) == set(documented(name).get("required", []))


@pytest.mark.parametrize(("name", "model"), ALL_CASES)
def test_the_contract_never_promises_less_than_the_implementation_demands(
    name: str, model: type[BaseModel]
) -> None:
    """A caller obeying the contract must always satisfy the validator.

    Deliberately a subset and not equality: the request schemas mark fields required that pydantic
    gives defaults to (``locale``, ``filters``, ``query_metrics``, ...). That direction is a
    stricter contract than the implementation, which is safe -- it only ever refuses payloads the
    adapter would have accepted. The reverse would let a contract-conformant client be rejected.
    """
    assert required_fields(model) <= set(documented(name).get("required", []))


# ---------------------------------------------------------------------------
# The model identity in particular
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(RESPONSE_MODELS))
def test_model_identity_is_documented_and_documented_as_optional(name: str) -> None:
    """The field this whole module exists because of.

    Optional is not a detail to be smoothed over: making it required in the contract would
    invalidate every archived response taken before it existed, and would tell client authors to
    expect a value that a provider declaring no model will never send.
    """
    schema = documented(name)

    assert "model" in schema["properties"], (
        "the response carries a model identity but the contract does not mention it; "
        "an evaluation artifact cannot cite a field no consumer knows to read"
    )
    assert "model" not in schema.get("required", [])


def test_model_identity_is_documented_as_absent_rather_than_null() -> None:
    """Absence and ``null`` are different bytes, and the Java client only handles one of them.

    ``AiAdapterClient.model`` reads a missing key as "nothing was recorded" and keeps null. It has
    no branch for a JSON ``null``. The adapter never emits one -- the routes serialise with
    ``response_model_exclude_none=True`` -- so the contract must say ``string`` and not
    ``["string", "null"]``, or it would be inviting a shape no consumer here handles.
    """
    identity = documented("ModelIdentity")

    assert identity["type"] == "string"
    for name in RESPONSE_MODELS:
        assert documented(name)["properties"]["model"] == {
            "$ref": "#/components/schemas/ModelIdentity"
        }


# ---------------------------------------------------------------------------
# The bytes that actually leave the process
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "payload", "schema_name"), LIVE_ENDPOINTS)
def test_every_key_a_live_response_emits_is_documented(
    path: str, payload: dict[str, Any], schema_name: str
) -> None:
    """One step past ``model_fields``: what the route serialises, not what the type allows.

    A response model and a handler can disagree -- a route can add a key, or a serialisation option
    can drop one -- and only reading the emitted body catches that. Runs against the default
    deterministic mock provider, so it needs no key and opens no socket.
    """
    response = client.post(path, json=payload)

    assert response.status_code == 200, response.text
    emitted = set(response.json())
    described = set(documented(schema_name)["properties"])

    assert emitted <= described, (
        f"{path} emitted undocumented keys {sorted(emitted - described)}; "
        f"with additionalProperties=false this response is invalid against its own contract"
    )
    # Not merely documented -- present. The mock declares an identity, so a run through the default
    # deployment must carry one; if it stopped doing so, artifacts would go back to being unable to
    # say which model produced them, and every check above would still pass.
    assert "model" in emitted
