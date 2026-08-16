"""``searchops-tools.openapi.json`` must describe the tool gateway that is actually running.

Same defect as ``test_contract_drift.py`` guards on the adapter side, one service over. The gateway
contract is the document an agent -- or the next person writing a client -- reads to find out what
operations exist and what they take. It had fallen behind in a way nothing could notice:
``POST /evaluations/candidate`` was live and undocumented, and with no ``EvaluationRequest`` schema
in the file, the ``use_ai`` and ``strategy_config`` properties were invisible too. A reader would
have concluded that evaluating an unpublished candidate config was impossible, and would have gone
back to the thing that endpoint exists to avoid: publishing a proposal in order to score it.

Why this check lives in the Python suite. It needs to read two things at once -- the checked-in
contract and the Java records that define the wire shape -- and it must do so without a JVM, a
database, or a running service, so that it stays a fast, unconditional check rather than one that
gets skipped in the environments where drift creeps in. Nothing on the Java side reads the contract
file at all today, which is exactly how the gap opened.

What is read, and how narrowly. Only two things are extracted from Java source: the request-mapping
paths on ``ToolGatewayController``, and the component names of the ``EvaluationRequest`` and
``EvaluationResult`` records (their ``@JsonProperty`` values where present, otherwise the component
name, which is what Jackson would use). Nothing here type-checks Java or interprets its semantics;
it compares two lists of wire names. When this test fails, the fix is nearly always one line of
JSON, and the failure message says which name is on which side.

Three kinds of drift it turns red:

* an endpoint added to the gateway and never written down (the ``/evaluations/candidate`` case);
* a request or response field added to a record and never written down (``strategy_config``);
* a contract field renamed out from under the implementation, or vice versa -- one rename shows up
  as both an unexpected and a missing name, in one failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[3]
CONTRACT = PLATFORM / "packages" / "api-contracts" / "openapi" / "searchops-tools.openapi.json"
JAVA = PLATFORM / "services" / "search-service" / "src" / "main" / "java" / "lab" / "searchops"
API_MODELS = JAVA / "domain" / "ApiModels.java"
GATEWAY = JAVA / "api" / "ToolGatewayController.java"
EVALUATION_SERVICE = JAVA / "service" / "EvaluationService.java"

DOCUMENT = json.loads(CONTRACT.read_text())
SCHEMAS = DOCUMENT["components"]["schemas"]

CANDIDATE_PATH = "/evaluations/candidate"

MAPPING = re.compile(r'@(?:Get|Post|Put|Delete|Patch)Mapping\(\s*"([^"]+)"')
JSON_PROPERTY = re.compile(r'@JsonProperty\("([^"]+)"\)')
TRAILING_IDENTIFIER = re.compile(r"(\w+)\s*$")


def read_java(path: Path) -> str:
    """Java source with comments removed, so annotations inside comments are never picked up."""
    assert path.exists(), (
        f"{path} is missing. This test compares {CONTRACT.name} against the Java records that "
        f"define its wire shape; without the source there is nothing to compare and contract "
        f"drift becomes undetectable again."
    )
    source = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def record_header(source: str, record: str) -> str:
    """The text between ``public record Name(`` and its matching close paren."""
    marker = f"public record {record}("
    assert marker in source, f"record {record} no longer exists in {API_MODELS.name}"
    index = source.index(marker) + len(marker)
    start, depth = index, 1
    while depth:
        char = source[index]
        if char == '"':
            index += 1
            while source[index] != '"':
                index += 2 if source[index] == "\\" else 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start:index]
        index += 1
    raise AssertionError(f"unbalanced parentheses in the {record} record header")


def split_components(header: str) -> list[str]:
    """Split a record header on its top-level commas.

    Depth tracking covers parentheses (annotation arguments, ``@Size(min = 1, max = 2)``) as well
    as angle brackets (generic arguments, ``Map<String, Double>``); a comma inside either separates
    arguments of one component, not two components.
    """
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in header:
        if char in "(<":
            depth += 1
        elif char in ")>":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return [stripped for stripped in (part.strip() for part in parts) if stripped]


def wire_names(record: str) -> list[str]:
    """The JSON key each component of a record serialises to, in declaration order."""
    names = []
    for component in split_components(record_header(read_java(API_MODELS), record)):
        renamed = JSON_PROPERTY.search(component)
        names.append(
            renamed.group(1) if renamed else TRAILING_IDENTIFIER.search(component).group(1)
        )
    return names


def candidate_operation() -> dict:
    assert CANDIDATE_PATH in DOCUMENT["paths"], (
        f"POST {CANDIDATE_PATH} is served by ToolGatewayController but absent from "
        f"{CONTRACT.name}; a client written from the contract cannot know it exists"
    )
    return DOCUMENT["paths"][CANDIDATE_PATH]["post"]


# ---------------------------------------------------------------------------
# Every operation the gateway serves is written down
# ---------------------------------------------------------------------------


def test_documented_paths_are_exactly_the_gateway_mappings() -> None:
    """Path-level drift, checked as equality rather than the subset the old test settled for.

    A subset assertion passes happily while the gateway grows endpoints nobody documents, which is
    precisely how ``/evaluations/candidate`` stayed invisible. The reverse direction matters too: a
    documented path with no mapping behind it is a 404 waiting for whoever trusts the contract.
    """
    served = set(MAPPING.findall(read_java(GATEWAY)))
    documented = set(DOCUMENT["paths"])

    assert served == documented, (
        f"served but undocumented={sorted(served - documented)}, "
        f"documented but not served={sorted(documented - served)}"
    )


def test_the_candidate_evaluation_is_documented_as_a_dry_run() -> None:
    """Its safety level is the first thing an agent reads, so the contract has to carry it.

    The endpoint sits next to ``/strategies/preview`` in what it is allowed to touch: it computes
    and writes nothing. Documenting it under the ``write`` tag, or leaving it untagged next to the
    approve/publish operations, would misrepresent an operation an agent may run freely as one that
    needs a human in the loop -- or the other way round, which is worse.
    """
    operation = candidate_operation()

    assert operation["tags"] == ["dry-run"]
    # Every mutating operation in this contract takes an Idempotency-Key, because a retry must not
    # produce a second strategy version. A read-only operation has nothing to be idempotent about,
    # and demanding the header would imply otherwise.
    assert "parameters" not in operation


# ---------------------------------------------------------------------------
# EvaluationRequest: the schema that was missing entirely
# ---------------------------------------------------------------------------


def test_evaluation_request_schema_matches_the_java_record_field_for_field() -> None:
    """The check that would have caught ``strategy_config`` and ``use_ai`` going undocumented."""
    implemented = set(wire_names("EvaluationRequest"))
    described = set(SCHEMAS["EvaluationRequest"]["properties"])

    assert implemented == described, (
        f"EvaluationRequest has drifted: "
        f"implemented but undocumented={sorted(implemented - described)}, "
        f"documented but not implemented={sorted(described - implemented)}. "
        f"Update {CONTRACT} alongside ApiModels.EvaluationRequest."
    )


def test_the_documented_property_order_follows_the_record() -> None:
    """Not cosmetic: it is what makes the two definitions checkable side by side by eye.

    A reviewer comparing the record against the schema in a diff should be reading two lists in the
    same order. Whoever adds the eighth component will then add the eighth property, instead of
    appending it wherever and leaving the next reader to sort out which of seven names moved.
    """
    assert list(SCHEMAS["EvaluationRequest"]["properties"]) == wire_names("EvaluationRequest")


@pytest.mark.parametrize("field", ["use_ai", "strategy_config", "use_rerank", "rerank_depth"])
def test_properties_added_after_the_first_release_are_documented_as_optional(field: str) -> None:
    """The compatibility promise, asserted rather than asserted-in-prose.

    Each of these has a Java wrapper type and a compact-constructor default precisely so that the
    payload ``data/scripts/evaluate.py`` has always sent -- queries/k/persist and nothing else --
    keeps deserialising and keeps reproducing the archived baselines. Documenting any of them as
    required would tell client authors the opposite of what the implementation does, and would
    retroactively declare every archived request invalid.
    """
    schema = SCHEMAS["EvaluationRequest"]

    assert field in schema["properties"]
    assert field not in schema["required"]


def test_only_the_three_fields_evaluate_py_sends_are_required() -> None:
    """``k`` and ``persist`` are Java primitives, and under Jackson 3 a missing primitive is a 400.

    So these three are required in fact, not by preference, and the contract has to say so or a
    client that omits ``persist`` gets a 400 the document gave it no reason to expect.
    """
    assert SCHEMAS["EvaluationRequest"]["required"] == ["queries", "k", "persist"]


def test_candidate_evaluation_makes_strategy_config_mandatory() -> None:
    """The guard rail that keeps an agent from scoring someone else's work as its own.

    ``EvaluationService.runCandidate`` rejects a body without ``strategy_config`` instead of
    quietly falling back to "evaluate the published strategy", because that fallback would hand
    back the published strategy's numbers under a heading that reads like the candidate's. The
    contract has to reproduce that, which is why the request body is the shared
    ``EvaluationRequest`` narrowed by an ``allOf`` rather than the schema on its own.
    """
    body = candidate_operation()["requestBody"]["content"]["application/json"]["schema"]
    branches = body["allOf"]

    assert {"$ref": "#/components/schemas/EvaluationRequest"} in branches
    assert any("strategy_config" in branch.get("required", []) for branch in branches)
    # And still optional on the shared schema, because /api/v1/evaluations/run legitimately omits
    # it -- that is how every published-strategy baseline in baselines/ was produced.
    assert "strategy_config" not in SCHEMAS["EvaluationRequest"]["required"]


# ---------------------------------------------------------------------------
# The response, and the evidence fields inside it
# ---------------------------------------------------------------------------


def test_candidate_result_schema_matches_the_java_record_field_for_field() -> None:
    """Including ``ai_model`` and ``rerank_model``, which are the point of the exercise.

    Those two are what lets an artifact in ``experiments/`` say which model produced its numbers.
    A field that exists but is undocumented is one nobody downstream knows to read, which leaves
    the evidence sitting in a file that no consumer was told to look in.
    """
    implemented = set(wire_names("EvaluationResult"))
    described = set(SCHEMAS["CandidateEvaluationResult"]["properties"])

    assert implemented == described, (
        f"CandidateEvaluationResult has drifted: "
        f"implemented but undocumented={sorted(implemented - described)}, "
        f"documented but not implemented={sorted(described - implemented)}. "
        f"Update {CONTRACT} alongside ApiModels.EvaluationResult."
    )


def test_everything_documented_as_always_present_is_a_real_field() -> None:
    """A required list is only meaningful if every name in it can actually appear."""
    schema = SCHEMAS["CandidateEvaluationResult"]

    assert set(schema["required"]) <= set(schema["properties"])


@pytest.mark.parametrize("field", ["ai_model", "rerank_model", "use_rerank", "rerank_depth"])
def test_observed_and_conditional_fields_are_not_documented_as_required(field: str) -> None:
    """Absent is a meaningful value here, and the contract must leave room for it.

    ``ai_model`` is *observed* from the responses of a run, not echoed from the request. When a run
    used no AI, or asked for it and degraded every time, nothing is observed and the key is omitted
    -- deliberately, rather than filled with a placeholder that an archive could not tell apart
    from a real model name. Documenting these as required would push a consumer toward inventing
    exactly that placeholder.
    """
    assert field not in SCHEMAS["CandidateEvaluationResult"]["required"]


def test_the_documented_sentinels_are_the_ones_the_service_actually_emits() -> None:
    """``-1`` and ``"candidate"`` are literals in the contract, so they can go stale on their own.

    They are how a stored result declares that it belongs to no published version. If someone
    changes the sentinel in ``EvaluationService`` and the contract keeps the old value, an archived
    candidate run and a published-strategy run become indistinguishable to anyone matching on the
    documented value -- and telling those two apart is the entire reason the fields exist.
    """
    source = read_java(EVALUATION_SERVICE)
    version = re.search(r"CANDIDATE_STRATEGY_VERSION\s*=\s*(-?\d+)", source)
    label = re.search(r'CANDIDATE_STRATEGY_SOURCE\s*=\s*"([^"]+)"', source)
    properties = SCHEMAS["CandidateEvaluationResult"]["properties"]

    assert version is not None and label is not None
    assert properties["strategy_version"]["const"] == int(version.group(1))
    assert properties["strategy_source"]["const"] == label.group(1)
