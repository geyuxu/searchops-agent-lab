"""Tests for the LangChain rerank provider.

Three constraints shape this file, and they are not the same three that shape
``test_providers_langchain.py``.

**No network, no key, no LangChain install required.** ``scripts/test-unit.sh`` does not source
``.env``, CI has no model credential, and the checked-in venv does not necessarily have
``langchain-openai`` in it. So the module is made importable with a synthetic ``langchain*``
package when the real one is absent, and every test then replaces ``ChatOpenAI`` *inside the
provider's own namespace*. Patching the provider's namespace rather than a shared ``sys.modules``
stub is deliberate: ``test_providers_langchain.py`` also installs LangChain stubs at import time,
and pytest imports every test module before running any test, so whichever file is collected
first wins the binding. A test that asserted against its own stub's recorder would be silently
vacuous whenever the other file got there first. Patching the namespace cannot be outrun that way.

**A reranker's failure mode is not the rewriter's.** Rewriting can emit a bad query; reranking
cannot change the candidate set at all, so the only two things that can go wrong are (a) the
order is worse, and (b) a candidate goes missing from the answer. (b) is the dangerous one: a
``ranked_product_ids`` that is one id short means a product Elasticsearch really did retrieve
never reaches the result page. That reads as a *recall* regression while the change that caused
it was a *ranking* change, so it is nearly unattributable. Most of this file is therefore one
assertion repeated against increasingly hostile model output: **the answer is always a
permutation of the candidates that were sent in.**

**Negation is tested as behaviour, not as prose.** ESCI queries invert on a single stopword, and
for a negated query every non-semantic signal — BM25 score, token overlap, embedding similarity —
points at the wrong answer, because the excluded product matches every other word. So there are
tests for the term extraction table (including the ``no. 2 pencils`` and ``free shipping`` traps,
where the naive reading of the cue word destroys the query), for the reminder reaching the
prompt, and for the demotion guard actually reordering the model's answer.

Nothing here asserts anything about model quality and nothing here produces a metric. Whether
reranking moves NDCG@10 against ``experiments/baseline-v7-holdout.json`` is a question for a real
evaluation run, not for a stub.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import inspect
import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import Candidate, RerankRequest, RerankResponse
from app.provider import MockProvider, Provider, load_provider

client = TestClient(main.app)

# Obviously not a credential, so a leak into a prompt, an explanation or an assertion message is
# unmistakable. `explanation` is handed back to callers verbatim.
FAKE_KEY = "unit-test-not-a-real-credential-0000"

CREDENTIAL_VARS = (
    "AI_API_KEY",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "ANTHROPIC_API_KEY",
)

# Unroutable on purpose: if anything ever really dialled these, it would fail rather than reach a
# vendor. They are set explicitly so the provider never falls back to its shipped default profile
# during a test — "I thought I was testing model A" is the same class of defect the provider's own
# defaults-applied WARN exists to prevent.
REQUIRED_CONFIG = {
    "AI_MODEL": "qwen-plus-2025-12-01",
    "AI_API_BASE_URL": "https://stub.invalid/compatible-mode/v1",
}

OPTIONAL_VARS = (
    "AI_TEMPERATURE",
    "AI_MAX_TOKENS",
    "AI_REQUEST_TIMEOUT_MS",
    "AI_MAX_RETRIES",
    "AI_EXTRA_BODY",
    "AI_STRUCTURED_OUTPUT_METHOD",
    "AI_API_KEY_ENV",
    "AI_RERANK_TOP_K",
    "AI_RERANK_MAX_CANDIDATES",
    "AI_RERANK_TITLE_CHARS",
    "AI_RERANK_BRAND_CHARS",
    "AI_RERANK_DESCRIPTION_CHARS",
)


# ---------------------------------------------------------------------------
# Make the module importable without a real LangChain
# ---------------------------------------------------------------------------
# Only enough of a stub to survive `from langchain_openai import ChatOpenAI` and
# `from langchain_core.messages import HumanMessage, SystemMessage` at import time. Every test
# then overrides ChatOpenAI in the provider's namespace, so nothing below depends on what these
# placeholders do — except the message classes, which must expose `.content` so the prompt can be
# inspected. If the real LangChain is installed, none of this is used at all.


class _StubMessage:
    def __init__(self, content: Any = "", **kwargs: Any) -> None:
        self.content = content
        self.additional_kwargs = kwargs


class _StubSymbol:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def _stub_getattr(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    if name.endswith("Message"):
        return _StubMessage
    return _StubSymbol


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        return _stub_getattr(name)


class _StubLoader:
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> _StubModule:
        return _StubModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        return None


class _StubFinder:
    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        root = fullname.split(".", 1)[0]
        if root == "langchain" or root.startswith("langchain_"):
            return importlib.machinery.ModuleSpec(fullname, _StubLoader(), is_package=True)
        return None


def _langchain_is_real() -> bool:
    try:
        importlib.import_module("langchain_openai")
        importlib.import_module("langchain_core.messages")
    except Exception:
        return False
    return True


LANGCHAIN_IS_REAL = _langchain_is_real()
if not LANGCHAIN_IS_REAL:
    sys.meta_path.insert(0, _StubFinder())
    for _name in ("langchain_core", "langchain_core.messages", "langchain_openai"):
        _module = _StubModule(_name)
        _module.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault(_name, _module)
    sys.modules["langchain_core"].messages = sys.modules["langchain_core.messages"]  # type: ignore[attr-defined]

from app.providers import langchain_rerank as rerank_module  # noqa: E402

PROVIDER_CLASS = rerank_module.LangChainRerankProvider
PROVIDER_DOTTED_PATH = "app.providers.langchain_rerank:LangChainRerankProvider"
MODULE_SOURCE = Path(rerank_module.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Recording double for the chat model
# ---------------------------------------------------------------------------


class Recorder:
    """What the provider asked the chat model to do, and what it gets back."""

    def __init__(self) -> None:
        self.factory_kwargs: dict[str, Any] = {}
        self.structured: list[tuple[tuple, dict]] = []
        self.invocations: list[Any] = []
        self.result: Any = None
        self.raises: BaseException | None = None

    @property
    def prompt(self) -> str:
        """Everything the provider put in front of the model, as one string."""
        chunks: list[str] = []
        for messages in self.invocations:
            for message in messages if isinstance(messages, list) else [messages]:
                chunks.append(str(getattr(message, "content", message)))
        return "\n".join(chunks)


def answer(order: Any, explanation: str = "stub judge note", reasoning: int = 0) -> dict[str, Any]:
    """The ``include_raw=True`` envelope a structured-output chain really returns."""
    return {
        "parsed": SimpleNamespace(order=order, explanation=explanation),
        "raw": SimpleNamespace(
            content=json.dumps({"order": order, "explanation": explanation}, default=str),
            usage_metadata={
                "input_tokens": 1800,
                "output_tokens": 40,
                "output_token_details": {"reasoning": reasoning},
            },
        ),
        "parsing_error": None,
    }


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Replace ChatOpenAI *in the provider's namespace* — see this module's docstring."""
    box = Recorder()

    class FakeChat:
        def __init__(self, **kwargs: Any) -> None:
            box.factory_kwargs.update(kwargs)

        def with_structured_output(self, *args: Any, **kwargs: Any) -> FakeChat:
            box.structured.append((args, kwargs))
            return self

        def invoke(self, messages: Any = None, *args: Any, **kwargs: Any) -> Any:
            box.invocations.append(messages)
            if box.raises is not None:
                raise box.raises
            return box.result

    monkeypatch.setattr(rerank_module, "ChatOpenAI", FakeChat)
    return box


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in CREDENTIAL_VARS:
        monkeypatch.setenv(name, FAKE_KEY)
    for name, value in REQUIRED_CONFIG.items():
        monkeypatch.setenv(name, value)
    for name in OPTIONAL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Everything configured *except* the credential.

    Not "an empty environment": with the model and endpoint also missing, the provider could
    legitimately complain about either of those first and this test would never actually check
    the credential. Isolating the one missing variable is what makes the assertion mean something.
    """
    for name, value in REQUIRED_CONFIG.items():
        monkeypatch.setenv(name, value)
    for name in OPTIONAL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real outbound connection becomes an immediate, attributable failure."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the provider opened a network connection; it must reach the model through "
            "LangChain (replaced here), not by issuing its own HTTP request"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse, raising=False)
    monkeypatch.setattr(socket, "create_connection", refuse, raising=False)


# ---------------------------------------------------------------------------
# Candidate fixtures
# ---------------------------------------------------------------------------
# Shaped like real ESCI rows: long keyword-stuffed titles, brand repeated inside the title,
# marketing copy in the description.

FENCE_CANDIDATES = [
    Candidate(
        product_id="B0HOLED001",
        title="Vinyl Privacy Fence Panel with Pre-Drilled Drainage Holes, 6ft Outdoor Garden",
        brand="YardCraft",
        description="Pre-drilled holes let water through. " * 40,
        bm25_score=31.4,
    ),
    Candidate(
        product_id="B0SOLID002",
        title="Solid Vinyl Privacy Fence Panel 6ft, Full Coverage Outdoor Garden Screen",
        brand="YardCraft",
        description="Solid panel, full privacy. " * 40,
        bm25_score=27.9,
    ),
    Candidate(
        product_id="B0NOHOLE03",
        title="Fence Panel No Holes Solid Privacy Screen for Chain Link, 6ft Garden",
        brand="FenceWorks",
        description="No holes at all. " * 40,
        bm25_score=24.1,
    ),
    Candidate(
        product_id="B0POST0004",
        title="Galvanized Steel Fence Post Anchor Spike, 24 inch, Pack of 4",
        brand="SteelYard",
        description="Post anchor. " * 40,
        bm25_score=18.6,
    ),
    Candidate(
        product_id="B0GATE0005",
        title="Garden Gate Latch Kit for Wooden Fence, Black Powder Coated",
        brand="LatchPro",
        description="Gate latch kit. " * 40,
        bm25_score=12.2,
    ),
]

FENCE_IDS = [candidate.product_id for candidate in FENCE_CANDIDATES]


def build() -> Provider:
    """Construct the provider from whatever the active env fixture set up."""
    return PROVIDER_CLASS()


def request_for(
    query: str = "fence without holes",
    candidates: list[Candidate] | None = None,
    request_id: str = "rerank-test",
) -> RerankRequest:
    return RerankRequest(
        query=query,
        candidates=list(candidates if candidates is not None else FENCE_CANDIDATES),
        request_id=request_id,
    )


def run(provider: Provider, **kwargs: Any) -> tuple[Any, Exception | None]:
    try:
        return provider.rerank(request_for(**kwargs)), None
    except Exception as exc:
        return None, exc


def assert_is_permutation(ranked: list[str], expected_ids: list[str]) -> None:
    """The single most important invariant in this file.

    A short ``ranked_product_ids`` silently drops a document Elasticsearch retrieved, which shows
    up in the evaluation as lost recall even though only ranking changed.
    """
    assert isinstance(ranked, list)
    assert len(ranked) == len(expected_ids), (
        f"rerank returned {len(ranked)} ids for {len(expected_ids)} candidates: {ranked!r}"
    )
    assert sorted(ranked) == sorted(expected_ids), (
        f"rerank returned a different multiset of ids than it was given: {ranked!r}"
    )


# ---------------------------------------------------------------------------
# 0. It is a Provider, it is loadable, and it really goes through LangChain
# ---------------------------------------------------------------------------


def test_provider_implements_the_abc() -> None:
    assert issubclass(PROVIDER_CLASS, Provider)
    assert PROVIDER_CLASS.name.strip()
    assert PROVIDER_CLASS.name != "mock"


def test_constructor_takes_no_arguments() -> None:
    """``load_provider`` calls ``provider_class()`` with no arguments, at import time."""
    assert list(inspect.signature(PROVIDER_CLASS.__init__).parameters) == ["self"]


def test_load_provider_returns_this_provider_when_configured(
    configured_env: Any, recorder: Recorder
) -> None:
    configured_env.setenv("AI_PROVIDER", PROVIDER_DOTTED_PATH)
    provider = load_provider()
    assert isinstance(provider, PROVIDER_CLASS)
    assert provider.name != "mock"


def test_the_model_is_reached_through_langchain_and_not_by_hand() -> None:
    """Static half of "it really uses LangChain".

    The runtime half is ``test_rerank_opens_no_socket``: together they say the call goes through
    the LangChain surface and that nothing else goes out. Asserting on the source is unusual, but
    the alternative — asserting on a stub the test itself installed — proves nothing about which
    library the provider imported.
    """
    assert "from langchain_openai import ChatOpenAI" in MODULE_SOURCE
    assert "from langchain_core.messages import" in MODULE_SOURCE
    assert "with_structured_output" in MODULE_SOURCE
    assert "import httpx" not in MODULE_SOURCE
    assert "import requests" not in MODULE_SOURCE


def test_rerank_opens_no_socket(configured_env: Any, recorder: Recorder, no_network: None) -> None:
    recorder.result = answer([2, 3, 1])
    provider = build()
    assert recorder.factory_kwargs, "no chat model was constructed"
    assert recorder.structured, "the provider did not ask for structured output"

    result, error = run(provider)
    assert error is None, f"happy path raised {error!r}"
    assert recorder.invocations, "the provider never invoked the model"
    assert_is_permutation(result[0], FENCE_IDS)


def test_structured_output_targets_a_pydantic_schema(
    configured_env: Any, recorder: Recorder
) -> None:
    """The order must be constrained by the tool schema, not scraped out of prose."""
    recorder.result = answer([1])
    build()
    (args, kwargs), *_ = recorder.structured
    schema = args[0] if args else kwargs.get("schema")
    assert schema is rerank_module.RankedOrder
    assert "order" in rerank_module.RankedOrder.model_fields
    # `order` must be declared before `explanation`: tool-calling generates fields in schema
    # order, so putting the commentary first pays latency before the useful part arrives.
    assert list(rerank_module.RankedOrder.model_fields) == ["order", "explanation"]


# ---------------------------------------------------------------------------
# 1. Missing credential must fail loudly, at construction, naming the variable
# ---------------------------------------------------------------------------


def test_missing_credential_raises_and_names_the_variable(
    no_credentials: Any, recorder: Recorder
) -> None:
    """A reranker that degrades silently poisons its own control experiment.

    Reranking is being measured against ``experiments/baseline-v7-holdout.json``
    (NDCG@10 0.4720), and a plausible real effect is a couple of points. A provider that shrugged
    at a missing key would return the BM25 order and reproduce that baseline almost exactly —
    which is indistinguishable, in the metric, from "the reranker did not help".
    """
    with pytest.raises(Exception) as caught:
        PROVIDER_CLASS()

    message = str(caught.value)
    assert [name for name in CREDENTIAL_VARS if name in message], (
        f"the error must name the missing environment variable so an operator can act on it; "
        f"got {message!r}"
    )
    assert FAKE_KEY not in message


def test_missing_credential_also_fails_through_load_provider(
    no_credentials: pytest.MonkeyPatch, recorder: Recorder
) -> None:
    no_credentials.setenv("AI_PROVIDER", PROVIDER_DOTTED_PATH)
    with pytest.raises(Exception):  # noqa: B017 - propagated from the provider's constructor
        load_provider()


def test_api_key_is_read_through_the_indirection_variable(
    no_credentials: pytest.MonkeyPatch, recorder: Recorder
) -> None:
    """``AI_API_KEY_ENV`` holds a variable *name*; the value is only ever read from os.environ.

    The indirection is what lets DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / ... stay where the vendor
    put them instead of being copied into a second variable, and it is why no vendor variable name
    appears anywhere in the provider's source.
    """
    no_credentials.setenv("AI_API_KEY_ENV", "SOME_VENDOR_KEY")
    with pytest.raises(Exception) as caught:
        PROVIDER_CLASS()
    assert "SOME_VENDOR_KEY" in str(caught.value), (
        "the failure must name the variable the operator actually has to set"
    )
    no_credentials.setenv("SOME_VENDOR_KEY", FAKE_KEY)
    provider = PROVIDER_CLASS()
    assert isinstance(provider, Provider)
    # The key reaches exactly one place — ChatOpenAI's api_key — and nothing else.
    leaked = {
        name: value for name, value in recorder.factory_kwargs.items() if value == FAKE_KEY
    }
    assert set(leaked) == {"api_key"}, f"the credential reached unexpected settings: {leaked!r}"


def test_bad_numeric_configuration_fails_loudly(configured_env: Any, recorder: Recorder) -> None:
    configured_env.setenv("AI_RERANK_TOP_K", "ten")
    with pytest.raises(RuntimeError, match="AI_RERANK_TOP_K"):
        PROVIDER_CLASS()


def test_bad_extra_body_fails_loudly(configured_env: Any, recorder: Recorder) -> None:
    configured_env.setenv("AI_EXTRA_BODY", "{not json")
    with pytest.raises(RuntimeError, match="AI_EXTRA_BODY"):
        PROVIDER_CLASS()
    configured_env.setenv("AI_EXTRA_BODY", "[1, 2]")
    with pytest.raises(RuntimeError, match="AI_EXTRA_BODY"):
        PROVIDER_CLASS()


def test_bad_structured_output_method_fails_loudly(
    configured_env: Any, recorder: Recorder
) -> None:
    configured_env.setenv("AI_STRUCTURED_OUTPUT_METHOD", "telepathy")
    with pytest.raises(RuntimeError, match="AI_STRUCTURED_OUTPUT_METHOD"):
        PROVIDER_CLASS()


# ---------------------------------------------------------------------------
# 2. Configuration reaches the chat model
# ---------------------------------------------------------------------------


def test_configuration_defaults(configured_env: Any, recorder: Recorder) -> None:
    build()
    kwargs = recorder.factory_kwargs
    assert float(kwargs["temperature"]) == 0.0, "temperature must default to 0 for reproducibility"
    assert kwargs["max_retries"] == 0, (
        "a retry inside the provider necessarily blows the caller's read budget; retrying belongs "
        "to whoever owns the larger time budget"
    )
    assert kwargs["model"] == REQUIRED_CONFIG["AI_MODEL"]
    assert kwargs["base_url"] == REQUIRED_CONFIG["AI_API_BASE_URL"]
    # Output length grows with top_k, so a fixed 256 would truncate a long permutation into a
    # half-order that silently falls back to BM25 for the tail.
    assert kwargs["max_tokens"] >= 256


def test_max_tokens_grows_with_top_k(configured_env: Any, recorder: Recorder) -> None:
    build()
    small = recorder.factory_kwargs["max_tokens"]
    configured_env.setenv("AI_RERANK_TOP_K", "0")  # full permutation
    configured_env.setenv("AI_RERANK_MAX_CANDIDATES", "100")
    build()
    large = recorder.factory_kwargs["max_tokens"]
    assert large > small


def test_configuration_overrides_reach_the_chat_model(
    configured_env: Any, recorder: Recorder
) -> None:
    configured_env.setenv("AI_TEMPERATURE", "0.35")
    configured_env.setenv("AI_MAX_TOKENS", "77")
    configured_env.setenv("AI_REQUEST_TIMEOUT_MS", "2500")
    configured_env.setenv("AI_MAX_RETRIES", "2")
    configured_env.setenv("AI_EXTRA_BODY", '{"enable_thinking": false}')
    build()
    kwargs = recorder.factory_kwargs
    assert float(kwargs["temperature"]) == 0.35
    assert kwargs["max_tokens"] == 77
    assert kwargs["timeout"] == 2.5, "the timeout must be forwarded in seconds"
    assert kwargs["max_retries"] == 2
    # The private thinking switch only works at the top level of the request body; nesting it one
    # level deeper is silently ignored by the vendor and the model keeps thinking.
    assert kwargs["extra_body"] == {"enable_thinking": False}


# ---------------------------------------------------------------------------
# 3. The permutation invariant, against hostile model output
# ---------------------------------------------------------------------------

HOSTILE_ORDERS = [
    pytest.param([1, 2, 3, 4, 5], id="perfect-permutation"),
    pytest.param([3, 1], id="partial"),
    pytest.param([], id="empty"),
    pytest.param([2, 2, 2, 3], id="duplicates"),
    pytest.param([0, -1, 6, 99, 2], id="out-of-range"),
    pytest.param(["3", "1"], id="numbers-as-strings"),
    pytest.param(["[4]", "candidate 2"], id="numbers-inside-text"),
    pytest.param([3.0, 1.0], id="floats"),
    pytest.param([True, False, 2], id="booleans"),
    pytest.param([None, {"id": 1}, ["nested"], 5], id="junk-entries"),
    pytest.param(list(range(1, 60)), id="longer-than-the-candidate-set"),
    pytest.param([5, 5, 5, 5, 5, 5, 5, 5], id="one-id-repeated-forever"),
]


@pytest.mark.parametrize("order", HOSTILE_ORDERS)
def test_result_is_always_a_permutation_of_the_candidates(
    configured_env: Any, recorder: Recorder, order: Any
) -> None:
    """Whatever the model says, every candidate comes back exactly once.

    The provider validates even though the Java caller will validate again. The two layers guard
    different things and neither subsumes the other: the provider is the only place that knows the
    model was speaking in candidate *numbers* (out-of-range is a trivially detectable error here
    and an undetectable one downstream, where everything is an opaque id), and it is the only
    place that can keep the ``/ai/rerank`` contract's promise for *every* caller rather than for
    the one caller that happens to re-implement the check.
    """
    recorder.result = answer(order)
    provider = build()
    result, error = run(provider)
    assert error is None, f"order {order!r} raised {error!r}"
    assert_is_permutation(result[0], FENCE_IDS)


def test_missing_numbers_are_appended_in_the_original_order(
    configured_env: Any, recorder: Recorder
) -> None:
    """The tail is BM25's answer, untouched — that is what makes a short model answer safe."""
    recorder.result = answer([3, 1])
    provider = build()
    ranked, _scores, _explanation = provider.rerank(request_for())
    assert ranked[:2] == [FENCE_IDS[2], FENCE_IDS[0]]
    assert ranked[2:] == [FENCE_IDS[1], FENCE_IDS[3], FENCE_IDS[4]]


def test_out_of_set_numbers_are_discarded_and_reported(
    configured_env: Any, recorder: Recorder
) -> None:
    recorder.result = answer([99, 2, 0, 3])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for())
    assert ranked[:2] == [FENCE_IDS[1], FENCE_IDS[2]]
    assert "out-of-range" in explanation, (
        f"a repaired answer must be distinguishable from a clean one; got {explanation!r}"
    )


def test_duplicate_numbers_are_deduplicated_and_reported(
    configured_env: Any, recorder: Recorder
) -> None:
    recorder.result = answer([4, 4, 4, 1])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for())
    assert ranked[:2] == [FENCE_IDS[3], FENCE_IDS[0]]
    assert "duplicate" in explanation


def test_top_k_bounds_how_much_of_the_answer_is_adopted(
    configured_env: Any, recorder: Recorder
) -> None:
    """Beyond position K the model's judgement has no effect on the metric but real variance.

    NDCG@10 / Recall@10 / P@10 only see ten positions, so adopting the model's opinion about
    positions 11+ buys nothing and risks displacing a labelled document for no measurable gain.
    """
    configured_env.setenv("AI_RERANK_TOP_K", "2")
    recorder.result = answer([5, 4, 3, 2, 1])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for())
    assert ranked[:2] == [FENCE_IDS[4], FENCE_IDS[3]]
    # Positions 3+ revert to BM25 order over the candidates the model's head did not claim.
    assert ranked[2:] == [FENCE_IDS[0], FENCE_IDS[1], FENCE_IDS[2]]
    assert "beyond-top-k" in explanation
    # The count in the explanation must survive truncation. Deriving it from the pre-truncation
    # repair statistics reads "adopted 2 of 2" here, which understates the candidate set and makes
    # the only operator-visible diagnostic channel wrong exactly when something did happen.
    assert f"adopted 2 of {len(FENCE_CANDIDATES)} candidates" in explanation, explanation


def test_top_k_zero_asks_for_a_full_permutation(configured_env: Any, recorder: Recorder) -> None:
    configured_env.setenv("AI_RERANK_TOP_K", "0")
    recorder.result = answer([5, 4, 3, 2, 1])
    provider = build()
    ranked, _scores, _explanation = provider.rerank(request_for())
    assert ranked == list(reversed(FENCE_IDS))


def test_scores_are_strictly_decreasing_and_aligned_with_the_order(
    configured_env: Any, recorder: Recorder
) -> None:
    """Downstream must not be able to re-sort by score into a different order than we returned."""
    recorder.result = answer([3, 1])
    provider = build()
    ranked, scores, explanation = provider.rerank(request_for())
    assert [score.product_id for score in scores] == ranked
    values = [score.score for score in scores]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values)
    assert all(0.0 < value <= 1.0 for value in values)
    assert "positional" in explanation, (
        "the scores are a function of rank, not calibrated relevance; a caller that blends them "
        "with BM25 scores must be told"
    )


def test_the_request_object_is_not_mutated(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([5, 4, 3, 2, 1])
    provider = build()
    payload = request_for()
    provider.rerank(payload)
    assert [candidate.product_id for candidate in payload.candidates] == FENCE_IDS


def test_single_candidate_is_handled(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([1])
    provider = build()
    ranked, scores, _explanation = provider.rerank(
        request_for(candidates=[FENCE_CANDIDATES[0]])
    )
    assert ranked == [FENCE_IDS[0]]
    assert scores[0].score == 1.0


def test_duplicate_product_ids_in_the_request_still_round_trip(
    configured_env: Any, recorder: Recorder
) -> None:
    """The contract does not forbid a repeated id, and the answer must still be the same multiset.

    This is why the whole pipeline works on candidate *positions* and converts to ids only at the
    very end: a set-of-ids implementation would quietly return four rows for five candidates.
    """
    duplicated = [*FENCE_CANDIDATES, FENCE_CANDIDATES[0]]
    recorder.result = answer([6, 1])
    provider = build()
    ranked, _scores, _explanation = provider.rerank(request_for(candidates=duplicated))
    assert_is_permutation(ranked, [candidate.product_id for candidate in duplicated])


# ---------------------------------------------------------------------------
# 4. Negation
# ---------------------------------------------------------------------------

# Each row is a real trap. The "keep" rows are the ones a naive cue-word reading gets wrong, and
# getting them wrong is worse than having no negation handling at all: it would demote exactly the
# products the shopper asked for.
EXTRACTION_CASES = [
    ("fence without holes", {"holes"}),
    ("lawnmower tires without rims", {"rims"}),
    ("# 2 pencils not sharpened", {"sharpened"}),
    ("shower curtain no liner", {"liner"}),
    ("sugar-free gum", {"sugar"}),
    ("water bottle free of bpa", {"bpa"}),
    ("non slip bath mat", {"slip"}),
    # Traps: the cue word is present but is not a negation.
    ("no. 2 pencils", set()),
    ("no 2 pencils", set()),
    ("cookies free shipping", set()),
    ("free returns laptop stand", set()),
    ("wireless keyboard", set()),
]


@pytest.mark.parametrize(("query", "expected"), EXTRACTION_CASES)
def test_excluded_term_extraction(query: str, expected: set[str]) -> None:
    assert PROVIDER_CLASS._excluded_terms(query) == expected, (
        f"{query!r} must yield {expected!r}; a wrong extraction is worse than none, because the "
        f"guard then demotes the products the shopper actually wants"
    )


def test_the_prompt_repeats_the_exclusion_after_the_candidate_list(
    configured_env: Any, recorder: Recorder
) -> None:
    """The general rule sits ~2000 tokens before the model starts generating; this does not.

    Every one of those intervening tokens is a product title, and for a negated query the titles
    are dense with the excluded attribute. A reminder in the last line is the cheapest available
    counterweight.
    """
    recorder.result = answer([2, 3])
    provider = build()
    provider.rerank(request_for("fence without holes"))
    prompt = recorder.prompt
    assert "This query excludes: holes" in prompt
    assert prompt.index("This query excludes") > prompt.index("[5]"), (
        "the reminder must come after the candidate list, not before it"
    )


def test_no_exclusion_notice_when_the_query_has_no_negation(
    configured_env: Any, recorder: Recorder
) -> None:
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for("vinyl fence panel"))
    assert "This query excludes" not in recorder.prompt


def test_guard_demotes_a_candidate_that_asserts_the_excluded_attribute(
    configured_env: Any, recorder: Recorder
) -> None:
    """The model put the holed fence first; the provider must not forward that.

    ``B0HOLED001`` matches "fence" and "holes" and carries the highest BM25 score, so every signal
    that is not an understanding of "without" prefers it. This is the concrete shape of the most
    damaging reranking error available on this dataset.
    """
    recorder.result = answer([1, 2, 3])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for("fence without holes"))
    assert ranked[0] != "B0HOLED001", (
        "a fence *with* drilled holes was ranked first for 'fence without holes'; that is the "
        "exact product the shopper excluded, and nothing downstream can tell it from a good rank"
    )
    assert ranked[:3] == ["B0SOLID002", "B0NOHOLE03", "B0HOLED001"]
    assert "Guard" in explanation
    assert_is_permutation(ranked, FENCE_IDS)


def test_guard_does_not_punish_a_title_that_itself_negates_the_attribute(
    configured_env: Any, recorder: Recorder
) -> None:
    """"Fence Panel No Holes" contains the word "holes" and is the *right* answer.

    A substring check would demote it — turning a guard meant to protect negated queries into the
    thing that breaks them.
    """
    recorder.result = answer([3, 1])
    provider = build()
    ranked, _scores, _explanation = provider.rerank(request_for("fence without holes"))
    assert ranked[0] == "B0NOHOLE03"


def test_guard_never_promotes_a_candidate_the_model_did_not_select(
    configured_env: Any, recorder: Recorder
) -> None:
    """The guard's blast radius is bounded to the head the model already vouched for.

    It may reorder those; it may not reach into the tail. Otherwise a mis-extracted exclusion term
    could pull a candidate BM25 ranked 80th into the top 10, which is a far larger downside than
    the one the guard exists to prevent.
    """
    recorder.result = answer([1, 4])
    provider = build()
    ranked, _scores, _explanation = provider.rerank(request_for("fence without holes"))
    assert ranked[:2] == ["B0POST0004", "B0HOLED001"], "the head is partitioned, in place"
    assert ranked[2:] == ["B0SOLID002", "B0NOHOLE03", "B0GATE0005"], (
        "the tail must stay in BM25 order; the guard must not promote from it"
    )


def test_guard_is_inert_without_a_negation(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([1, 2, 3])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for("vinyl fence panel"))
    assert ranked[:3] == ["B0HOLED001", "B0SOLID002", "B0NOHOLE03"]
    assert "Guard" not in explanation


def test_guard_is_inert_when_every_selected_candidate_asserts_the_attribute(
    configured_env: Any, recorder: Recorder
) -> None:
    """A documented gap, pinned so it cannot change silently.

    With nothing clean in the head there is nothing to partition, and the guard deliberately will
    not reach into the tail to find one. Closing this would require exactly the unbounded
    promotion the previous test forbids.
    """
    recorder.result = answer([1])
    provider = build()
    ranked, _scores, explanation = provider.rerank(request_for("fence without holes"))
    assert ranked[0] == "B0HOLED001"
    assert "Guard" not in explanation


# ---------------------------------------------------------------------------
# 5. Bad model output and model failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [None, "1, 2, 3", 42, {"first": 1}],
    ids=["none", "prose", "scalar", "mapping"],
)
def test_an_unusable_order_raises_instead_of_returning_the_bm25_order(
    configured_env: Any, recorder: Recorder, order: Any
) -> None:
    """Returning the input order here would be recorded as a *successful* rerank.

    The evaluation would then attribute a pure-BM25 run to the reranker. A 500 is uglier and
    honest: the caller records a fallback and the number never enters the comparison.
    """
    recorder.result = answer(order)
    provider = build()
    _result, error = run(provider)
    assert error is not None, "an unusable order must not be laundered into the BM25 order"


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("stubbed model read timeout"),
        ConnectionError("stubbed transport failure"),
        RuntimeError("stubbed provider-side error"),
    ],
    ids=["timeout", "transport", "runtime"],
)
def test_model_failures_propagate_instead_of_degrading_silently(
    configured_env: Any, recorder: Recorder, failure: BaseException
) -> None:
    recorder.result = answer([1, 2])
    recorder.raises = failure
    provider = build()
    _result, error = run(provider)
    assert error is not None, (
        f"rerank swallowed {type(failure).__name__}; the caller would record a successful AI "
        f"rerank that never reached a model"
    )


def test_failure_logs_do_not_carry_the_prompt_or_the_key(
    configured_env: Any, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Adapter logs are long-lived; the rerank request body is a whole page of catalogue text."""
    recorder.result = answer([1])
    recorder.raises = RuntimeError(f"upstream said no, key={FAKE_KEY}, body=<2000 chars>")
    provider = build()
    with caplog.at_level("WARNING", logger="ai-adapter"):
        _result, error = run(provider)
    assert error is not None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_KEY not in logged
    assert "Vinyl Privacy Fence Panel" not in logged
    assert "ai.rerank.failed" in logged


def test_thinking_mode_is_reported(
    configured_env: Any, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Thinking mode does not fail; it just gets slow enough to time out the whole evaluation.

    Nothing at the HTTP layer looks wrong, so it has to be caught from the usage metadata rather
    than by someone noticing the latency.
    """
    recorder.result = answer([1, 2], reasoning=512)
    provider = build()
    with caplog.at_level("WARNING", logger="ai-adapter"):
        provider.rerank(request_for())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ai.rerank.thinking_enabled" in logged
    assert "enable_thinking" in logged


# ---------------------------------------------------------------------------
# 6. Prompt size control
# ---------------------------------------------------------------------------


def test_descriptions_are_not_sent_by_default(configured_env: Any, recorder: Recorder) -> None:
    """``Candidate.description`` is capped at 5000 chars by the contract: 50 of them is 250KB.

    ESCI's Exact judgement is carried by the title (product type, brand, model, count, size); the
    description is mostly repeated marketing copy. Sending it multiplies latency for signal we
    have no evidence exists.
    """
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for())
    assert "Pre-drilled holes let water through" not in recorder.prompt


def test_descriptions_can_be_switched_on(configured_env: Any, recorder: Recorder) -> None:
    configured_env.setenv("AI_RERANK_DESCRIPTION_CHARS", "40")
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for())
    assert "Pre-drilled holes" in recorder.prompt


def test_titles_are_truncated_on_a_word_boundary(configured_env: Any, recorder: Recorder) -> None:
    configured_env.setenv("AI_RERANK_TITLE_CHARS", "30")
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for())
    prompt = recorder.prompt
    assert "Vinyl Privacy Fence Panel" in prompt
    assert "Pre-Drilled" not in prompt
    for line in prompt.splitlines():
        if line.startswith("["):
            head = line.split("|")[0]
            assert len(head) <= 30 + 8, f"title line was not truncated: {line!r}"


def test_candidates_are_numbered_from_one_and_carry_brand_only_when_new(
    configured_env: Any, recorder: Recorder
) -> None:
    """Brands are repeated inside ESCI titles; sending them twice is pure token waste."""
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for())
    prompt = recorder.prompt
    for number in range(1, len(FENCE_CANDIDATES) + 1):
        assert f"[{number}] " in prompt
    # "YardCraft" is not in the title of candidate 1, so it must be appended.
    assert "YardCraft" in prompt
    # ...but never twice on the same line.
    lines = [line for line in prompt.splitlines() if line.startswith("[1] ")]
    assert len(lines) == 1
    assert lines[0].count("YardCraft") == 1


def test_the_prompt_never_carries_the_credential(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([1])
    provider = build()
    provider.rerank(request_for())
    assert FAKE_KEY not in recorder.prompt
    assert "fence without holes" in recorder.prompt


def test_candidate_truncation_is_visible_not_silent(
    configured_env: Any, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """Judging 2 of 5 while the operator believes all 5 were judged is unfalsifiable in the metric.

    The untouched remainder still has to come back, in BM25 order — truncating the prompt must
    never truncate the answer.
    """
    configured_env.setenv("AI_RERANK_MAX_CANDIDATES", "2")
    recorder.result = answer([2, 1])
    provider = build()
    with caplog.at_level("WARNING", logger="ai-adapter"):
        ranked, _scores, explanation = provider.rerank(request_for())
    assert "[3]" not in recorder.prompt
    assert ranked == [FENCE_IDS[1], FENCE_IDS[0], FENCE_IDS[2], FENCE_IDS[3], FENCE_IDS[4]]
    assert_is_permutation(ranked, FENCE_IDS)
    assert "AI_RERANK_MAX_CANDIDATES" in explanation
    assert "adopted 2 of 2 candidates" in explanation, (
        f"the count must describe what the model was actually shown, not the request size; "
        f"got {explanation!r}"
    )
    assert "ai.rerank.candidates_truncated" in "\n".join(
        record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 7. Contract compliance and the untouched neighbours
# ---------------------------------------------------------------------------


def test_result_validates_as_the_response_model(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([2, 1])
    provider = build()
    ranked, scores, explanation = provider.rerank(request_for())
    response = RerankResponse(
        ranked_product_ids=ranked,
        scores=scores,
        explanation=explanation,
        provider=provider.name,
        latency_ms=0,
    )
    assert response.ranked_product_ids == ranked
    assert FAKE_KEY not in response.explanation


def test_http_surface_reports_this_provider(configured_env: Any, recorder: Recorder) -> None:
    recorder.result = answer([2, 1])
    provider = build()
    configured_env.setattr(main, "provider", provider)
    response = client.post(
        "/ai/rerank",
        json={
            "query": "fence without holes",
            "candidates": [
                {
                    "product_id": candidate.product_id,
                    "title": candidate.title,
                    "brand": candidate.brand,
                    "description": "",
                    "bm25_score": candidate.bm25_score,
                }
                for candidate in FENCE_CANDIDATES
            ],
            "request_id": "rerank-http-1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    RerankResponse.model_validate(body)
    assert set(body) == {
        "ranked_product_ids",
        "scores",
        "explanation",
        "provider",
        "latency_ms",
    }
    assert body["provider"] == provider.name != "mock"
    assert_is_permutation(body["ranked_product_ids"], FENCE_IDS)
    assert FAKE_KEY not in json.dumps(body)


def test_rewrite_and_suggest_delegate_verbatim_to_the_mock_provider(
    configured_env: Any, recorder: Recorder
) -> None:
    """One behaviour change per increment, so an evaluation delta has one candidate explanation.

    If this provider also rewrote queries, a difference against the v7 baseline could come from
    rewriting or from reranking and no amount of staring at the number would separate them.
    """
    from app.models import QueryRewriteRequest, StrategySuggestRequest

    provider = build()
    mock = MockProvider()

    rewrite_payload = QueryRewriteRequest(query="cheap tv under $300", request_id="delegate-1")
    assert provider.rewrite(rewrite_payload) == mock.rewrite(rewrite_payload)

    suggest_payload = StrategySuggestRequest(
        query_metrics=[{"query": "fence without holes", "zero_result": True, "ndcg10": 0.0}],
        request_id="delegate-2",
    )
    assert provider.suggest(suggest_payload) == mock.suggest(suggest_payload)
    assert not recorder.invocations, "delegation must not reach the model"


def test_default_provider_is_untouched_by_this_module() -> None:
    """``scripts/test-unit.sh`` does not source ``.env``: the default path stays keyless."""
    assert main.provider.name == "mock"
