"""Tests for the LangChain query-rewrite provider.

Three constraints shape every choice in this file.

**No network, no key.** ``scripts/test-unit.sh`` does not source ``.env`` and CI has no model
credential, so a test that reaches a model is a test that fails on every machine but the one it
was written on. The LangChain surface is therefore replaced by a stub installed into
``sys.modules`` (see ``_install_langchain_stubs``), and the tests that matter additionally poison
``socket.connect`` so a provider that bypasses LangChain and posts its own HTTP request fails
loudly instead of quietly reaching the internet from a unit test.

**Silence is the enemy.** The defect this whole increment exists to prevent is a degradation
that still returns HTTP 200: the Java client used to hardcode ``provider == "mock"`` and fall
back to BM25 for every real provider while the response looked healthy. The provider-side
version of that failure is a constructor that shrugs at a missing key, or a ``rewrite`` that
swallows a model timeout and returns the original query. Both would make an AI evaluation run
look like "AI does not help" when in truth the model was never consulted. So the assertions here
are mostly about *failing*: missing credential must raise, model errors must propagate, blank or
unparseable model output must never become a blank ``rewritten_query``.

**The provider is discovered, not hardcoded.** ``_discover_provider`` scans
``app/providers/*.py`` for a module whose name mentions LangChain and picks the ``Provider``
subclass defined in it, so renaming the module or the class does not silently disable this file.
If nothing is found, ``test_langchain_provider_module_exists`` *fails* — it does not skip. A
skipped contract test is the same silent-degradation pattern the contract is meant to outlaw.

Nothing here asserts anything about model quality, and nothing here produces a metric. Whether
a rewrite improves NDCG@10 over the archived BM25 baseline is a question for ``make evaluate-ai``
against the real model, not for a stub.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.machinery
import inspect
import json
import re
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import QueryRewriteRequest, QueryRewriteResponse
from app.provider import Provider, load_provider

client = TestClient(main.app)

# A value that is obviously not a credential, so a leak into an explanation, a prompt or an
# assertion message is unmistakable. Never put a real key in this file or in the environment a
# test builds; `explanation` is returned to the storefront verbatim.
FAKE_KEY = "unit-test-not-a-real-credential-0000"

# Every credential variable a reasonable implementation might read. Tests set *all* of them at
# once (or clear all of them at once) so this file does not silently depend on which one the
# provider chose. The documented one is AI_API_KEY (docs/ai-handoff.md, "Environment variables":
# "Empty means unconfigured — a real provider should fail loudly at startup").
CREDENTIAL_VARS = (
    "AI_API_KEY",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "ANTHROPIC_API_KEY",
)

# Configuration a provider may legitimately insist on rather than defaulting. Refusing to
# default the model name and the endpoint is a defensible choice — an implicit default is how
# "I thought I was evaluating model A" becomes "I was evaluating model B" with no visible sign —
# so the fixtures supply both instead of asserting they are optional. The values are unroutable
# on purpose: if anything ever really dialled them, it would fail rather than reach a vendor.
REQUIRED_CONFIG = {
    "AI_MODEL": "qwen-plus-2025-12-01",
    "AI_API_BASE_URL": "https://stub.invalid/compatible-mode/v1",
}

# Knobs with documented defaults (docs/ai-handoff.md, ".env.example"), plus provider-specific
# extras. Cleared before every test so an ambient value in the developer's shell cannot change
# what the assertions mean.
OPTIONAL_VARS = (
    "AI_TEMPERATURE",
    "AI_MAX_TOKENS",
    "AI_REQUEST_TIMEOUT_MS",
    "AI_MAX_RETRIES",
    "AI_EXTRA_BODY",
    "AI_STRUCTURED_OUTPUT_METHOD",
    "AI_API_KEY_ENV",
)


# ---------------------------------------------------------------------------
# LangChain stub
# ---------------------------------------------------------------------------
# The stub is installed once, at import time, and its behaviour is steered through the
# module-level BEHAVIOUR object rather than through fresh objects per test. That is deliberate:
# a provider is free to bind `init_chat_model` (or a chat class) at module import time, which
# captures whatever object existed then. Swapping the stub per test would leave such a provider
# holding a stale reference and every assertion about "did it call the model" would be vacuous.
# A stable stub with mutable behaviour cannot be outrun that way.


@dataclasses.dataclass
class _Behaviour:
    """What the stubbed chat model does next, and what it has been asked to do so far."""

    result: Any = None
    raises: BaseException | None = None
    factory_calls: list[tuple[tuple, dict]] = dataclasses.field(default_factory=list)
    config_calls: list[tuple[str, tuple, dict]] = dataclasses.field(default_factory=list)
    invoke_calls: list[Any] = dataclasses.field(default_factory=list)

    def reset(self) -> None:
        self.result = None
        self.raises = None
        self.factory_calls.clear()
        self.config_calls.clear()
        self.invoke_calls.clear()


BEHAVIOUR = _Behaviour()


class _LlmResult(dict):
    """One canned model answer, shaped to satisfy every plausible way of reading it.

    A provider may use ``with_structured_output(SomeModel)`` and read ``out.rewritten_query``;
    or a plain call and read ``out.content`` as JSON; or a JSON output parser and read
    ``out["rewritten_query"]``. All three work against this object, so the tests constrain the
    provider's *behaviour* without dictating its LangChain composition style.
    """

    def __init__(self, payload: dict[str, Any], content: str) -> None:
        super().__init__(payload)
        self._content = content

    @property
    def content(self) -> str:
        return self._content

    def text(self) -> str:
        return self._content

    def model_dump(self) -> dict[str, Any]:
        return dict(self)

    def json(self) -> str:
        return self._content

    def __str__(self) -> str:
        return self._content

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _FakeRaw:
    """The unparsed AIMessage that ``include_raw=True`` returns alongside the parsed draft."""

    def __init__(self, content: str, reasoning_tokens: int = 0) -> None:
        self.content = content
        self.usage_metadata = {
            "input_tokens": 113,
            "output_tokens": 24,
            "output_token_details": {"reasoning": reasoning_tokens},
        }


def llm_answer(
    rewritten: str | None = None,
    confidence: float = 0.9,
    *,
    reasoning_tokens: int = 0,
    **extra: Any,
) -> _LlmResult:
    """Build a well-formed answer that satisfies every structured-output style at once.

    ``with_structured_output(schema)`` yields the draft directly, while adding
    ``include_raw=True`` yields an envelope of ``{"raw", "parsed", "parsing_error"}`` instead.
    The returned object *is* that envelope and *also* carries the draft's own fields, so a
    provider reading ``envelope["parsed"].rewritten_query``, ``answer.rewritten_query`` or
    ``json.loads(answer.content)`` all see the same canned answer.
    """
    core: dict[str, Any] = {}
    if rewritten is not None:
        core["rewritten_query"] = rewritten
        core["query"] = rewritten
        core["rewrite"] = rewritten
    core["confidence"] = confidence
    core.setdefault("extracted_filters", {})
    core.setdefault("explanation", "stubbed langchain answer")
    core.update(extra)

    content = json.dumps(core)
    envelope = dict(core)
    envelope["parsed"] = _LlmResult(core, content)
    envelope["raw"] = _FakeRaw(content, reasoning_tokens)
    envelope["parsing_error"] = None
    return _LlmResult(envelope, content)


def llm_raw(content: str) -> _LlmResult:
    """An answer the schema layer could not parse: text present, ``parsed`` empty.

    This is the shape a real ``include_raw=True`` chain produces when the model ignores the
    schema, and the shape a plain chain produces when the model returns prose instead of JSON.
    """
    return _LlmResult(
        {"parsed": None, "raw": _FakeRaw(content), "parsing_error": ValueError("not schema")},
        content,
    )


class _FakeChatModel:
    """Stands in for whatever LangChain chat model the provider builds."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        BEHAVIOUR.factory_calls.append((args, kwargs))

    # Every LCEL decorator returns self, so a provider may chain as many as it likes.
    def _record(self, label: str, args: tuple, kwargs: dict) -> _FakeChatModel:
        BEHAVIOUR.config_calls.append((label, args, kwargs))
        return self

    def with_structured_output(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("with_structured_output", a, k)

    def bind(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("bind", a, k)

    def bind_tools(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("bind_tools", a, k)

    def with_retry(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("with_retry", a, k)

    def with_config(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("with_config", a, k)

    def with_fallbacks(self, *a: Any, **k: Any) -> _FakeChatModel:
        return self._record("with_fallbacks", a, k)

    def __or__(self, other: Any) -> _FakeChain:
        return _FakeChain([self, other])

    def __ror__(self, other: Any) -> _FakeChain:
        return _FakeChain([other, self])

    def invoke(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        BEHAVIOUR.invoke_calls.append(value)
        if BEHAVIOUR.raises is not None:
            raise BEHAVIOUR.raises
        return BEHAVIOUR.result

    # Aliases a provider might reach for instead of invoke().
    __call__ = invoke
    predict = invoke
    predict_messages = invoke

    def batch(self, values: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [self.invoke(value) for value in values]

    def stream(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        yield self.invoke(value)


class _FakeChain:
    """Supports ``prompt | llm | parser`` without pulling in real LCEL."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)

    def __or__(self, other: Any) -> _FakeChain:
        return _FakeChain([*self.steps, other])

    def invoke(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        for step in self.steps:
            invoke = getattr(step, "invoke", None)
            if callable(invoke):
                value = invoke(value)
        return value


class _FakePassthrough:
    """A prompt template or output parser: records nothing, changes nothing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    @classmethod
    def from_messages(cls, messages: Any = (), **kwargs: Any) -> _FakePassthrough:
        return cls(messages, **kwargs)

    @classmethod
    def from_template(cls, template: Any = "", **kwargs: Any) -> _FakePassthrough:
        return cls(template, **kwargs)

    def format(self, **kwargs: Any) -> str:
        return json.dumps(kwargs, default=str)

    def format_messages(self, **kwargs: Any) -> list[Any]:
        return [_FakeMessage(json.dumps(kwargs, default=str))]

    def invoke(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        return value

    def __or__(self, other: Any) -> _FakeChain:
        return _FakeChain([self, other])


class _FakeMessage:
    def __init__(self, content: Any = "", **kwargs: Any) -> None:
        self.content = content
        self.additional_kwargs = kwargs

    def __repr__(self) -> str:  # shows up in prompt-inspection assertions
        return f"{type(self).__name__}({self.content!r})"


class _StubSymbol:
    """Fallback for any LangChain name the tests did not anticipate."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __or__(self, other: Any) -> _FakeChain:
        return _FakeChain([self, other])

    def invoke(self, value: Any = None, *args: Any, **kwargs: Any) -> Any:
        return value


def _init_chat_model(*args: Any, **kwargs: Any) -> _FakeChatModel:
    """Stand-in for ``langchain.chat_models.init_chat_model``.

    This is the entry point ``agent/searchops_agent/proposers.py`` already uses, so it is the
    likeliest one here too; ``langchain_openai.ChatOpenAI`` is covered by ``_module_getattr``.
    """
    return _FakeChatModel(*args, **kwargs)


_EXPLICIT_SYMBOLS: dict[str, Any] = {
    "init_chat_model": _init_chat_model,
    "ChatPromptTemplate": _FakePassthrough,
    "PromptTemplate": _FakePassthrough,
    "HumanMessagePromptTemplate": _FakePassthrough,
    "SystemMessagePromptTemplate": _FakePassthrough,
    "StrOutputParser": _FakePassthrough,
    "JsonOutputParser": _FakePassthrough,
    "PydanticOutputParser": _FakePassthrough,
    "BaseMessage": _FakeMessage,
    "AIMessage": _FakeMessage,
    "HumanMessage": _FakeMessage,
    "SystemMessage": _FakeMessage,
}


def _module_getattr(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    if name in _EXPLICIT_SYMBOLS:
        return _EXPLICIT_SYMBOLS[name]
    # Anything that looks like a chat model becomes the recording fake, so a provider that
    # imports ChatOpenAI / ChatTongyi / ChatAnthropic is still observed.
    if name.startswith("Chat") or name.endswith(("LLM", "ChatModel")) or name == "OpenAI":
        return _FakeChatModel
    return _StubSymbol


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        return _module_getattr(name)


class _StubLoader:
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> _StubModule:
        return _StubModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        return None


class _StubFinder:
    """Synthesises any ``langchain*`` module on demand, so an unanticipated import still works."""

    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        root = fullname.split(".", 1)[0]
        if root == "langchain" or root.startswith("langchain_"):
            return importlib.machinery.ModuleSpec(fullname, _StubLoader(), is_package=True)
        return None


_PREREGISTERED = (
    "langchain",
    "langchain.chat_models",
    "langchain.prompts",
    "langchain.schema",
    "langchain_core",
    "langchain_core.messages",
    "langchain_core.prompts",
    "langchain_core.output_parsers",
    "langchain_core.runnables",
    "langchain_core.language_models",
    "langchain_core.exceptions",
    "langchain_openai",
    "langchain_community",
    "langchain_community.chat_models",
)

_SAVED_MODULES: dict[str, types.ModuleType | None] = {}


def _install_langchain_stubs() -> None:
    """Put the stub in front of any real LangChain, then pre-register the common module names."""
    sys.meta_path.insert(0, _StubFinder())
    for name in _PREREGISTERED:
        _SAVED_MODULES[name] = sys.modules.get(name)
        module = _StubModule(name)
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module
    # Parent packages must expose their children as attributes for `from x.y import z`.
    for name in _PREREGISTERED:
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(sys.modules[parent], child, sys.modules[name])


_install_langchain_stubs()


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------

PROVIDERS_DIR = Path(main.__file__).resolve().parent / "providers"
EXPECTED_DOTTED_PATH = "app.providers.langchain_rewrite:LangChainRewriteProvider"


def _discover_provider() -> tuple[type[Provider] | None, str, str]:
    """Find the LangChain provider without pinning its module or class name.

    Returns ``(class, dotted_path, diagnostic)``. The diagnostic is what a failing
    ``test_langchain_provider_module_exists`` prints, so it has to be specific enough to act on.
    """
    modules = sorted(p.stem for p in PROVIDERS_DIR.glob("*.py") if "lang" in p.stem.lower())
    if not modules:
        present = sorted(p.name for p in PROVIDERS_DIR.glob("*.py"))
        return (
            None,
            "",
            f"no LangChain provider module in {PROVIDERS_DIR} (found: {present}). "
            f"Expected a module whose name contains 'lang', e.g. {EXPECTED_DOTTED_PATH}.",
        )
    problems: list[str] = []
    for stem in modules:
        dotted_module = f"app.providers.{stem}"
        try:
            module = importlib.import_module(dotted_module)
        except Exception as exc:  # a provider that cannot even import is a real finding
            problems.append(f"{dotted_module}: import failed: {type(exc).__name__}: {exc}")
            continue
        for attribute in vars(module).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, Provider)
                and attribute is not Provider
                and attribute.__module__ == module.__name__
            ):
                return attribute, f"{dotted_module}:{attribute.__name__}", ""
        problems.append(f"{dotted_module}: no app.provider.Provider subclass defined in it")
    return None, "", "; ".join(problems)


PROVIDER_CLASS, PROVIDER_DOTTED_PATH, PROVIDER_DIAGNOSTIC = _discover_provider()

requires_provider = pytest.mark.skipif(
    PROVIDER_CLASS is None,
    reason="LangChain provider not present; test_langchain_provider_module_exists reports it",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_behaviour() -> Any:
    BEHAVIOUR.reset()
    yield
    BEHAVIOUR.reset()


@pytest.fixture(scope="module", autouse=True)
def _restore_sys_modules() -> Any:
    """Leave sys.modules as found, so a future real LangChain install is not shadowed."""
    yield
    for name, original in _SAVED_MODULES.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _StubFinder)]


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A machine where the provider is configured — with a credential that is not a credential."""
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

    Deliberately not "an empty environment": a provider may reasonably refuse to start for
    several reasons, and if model and endpoint were also missing, this test would pass on
    whichever complaint happened to come first and would never actually check the credential.
    Isolating the one missing variable is what makes the assertion about the key mean something.
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
    """Make any real outbound connection an immediate, attributable failure.

    This is the load-bearing half of "it actually went through LangChain": the stub proves the
    LangChain surface was used, and this proves nothing else was.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "the provider opened a network connection; it must call the model through "
            "LangChain (which is stubbed here), not by issuing its own HTTP request"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse, raising=False)
    monkeypatch.setattr(socket, "create_connection", refuse, raising=False)


def build_provider() -> Provider:
    assert PROVIDER_CLASS is not None
    return PROVIDER_CLASS()


def request_for(query: str, **kwargs: Any) -> QueryRewriteRequest:
    kwargs.setdefault("request_id", "langchain-test")
    return QueryRewriteRequest(query=query, **kwargs)


def rewrite(provider: Provider, query: str, **kwargs: Any) -> tuple[Any, Exception | None]:
    """Call rewrite and report the outcome, so a test can accept "raised" *or* "guarded"."""
    try:
        return provider.rewrite(request_for(query, **kwargs)), None
    except Exception as exc:
        return None, exc


NEGATION_CUES = frozenset(
    {"without", "not", "no", "non", "none", "exclude", "excluding", "except", "minus", "free"}
)


def negation_survives(text: str) -> bool:
    """True if the text still carries an exclusion, in words or as a leading ``-``/``!``.

    ESCI queries such as "fence without holes" and "# 2 pencils not sharpened" invert meaning on
    a single stopword. A rewrite that drops it retrieves exactly the products the shopper is
    trying to avoid, which reads as a relevance *regression* in the evaluation.
    """
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    if NEGATION_CUES & tokens:
        return True
    return bool(re.search(r"(?:^|\s)[-!](?=\w)", text))


def recorded_arguments() -> list[Any]:
    """Every value the provider handed to the LangChain surface, flattened one level."""
    values: list[Any] = []
    for args, kwargs in BEHAVIOUR.factory_calls:
        values.extend(args)
        values.extend(kwargs.values())
    for _label, args, kwargs in BEHAVIOUR.config_calls:
        values.extend(args)
        values.extend(kwargs.values())
    flattened: list[Any] = []
    for value in values:
        if isinstance(value, dict):
            flattened.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            flattened.extend(value)
        else:
            flattened.append(value)
    return values + flattened


def recorded_kwargs() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for _args, kwargs in BEHAVIOUR.factory_calls:
        merged.update(kwargs)
    for _label, _args, kwargs in BEHAVIOUR.config_calls:
        merged.update(kwargs)
    for value in list(merged.values()):
        if isinstance(value, dict):
            merged.update(value)
    return merged


def mentions(needle: str) -> bool:
    return any(needle in str(value) for value in recorded_arguments())


def prompt_text() -> str:
    return " ".join(repr(call) for call in BEHAVIOUR.invoke_calls)


# ---------------------------------------------------------------------------
# 0. The provider exists at all
# ---------------------------------------------------------------------------


def test_langchain_provider_module_exists() -> None:
    """Fails — never skips — when the provider is missing.

    A skipped contract test is indistinguishable from a passing one on a dashboard, which is the
    same silent-degradation shape as the Java client that used to reject every non-mock provider
    while still answering 200. If this is red, everything else in this file is skipped and the
    LangChain path is unverified.
    """
    assert PROVIDER_CLASS is not None, PROVIDER_DIAGNOSTIC
    assert issubclass(PROVIDER_CLASS, Provider)


# ---------------------------------------------------------------------------
# 1. Missing credential must fail loudly, at construction, naming the variable
# ---------------------------------------------------------------------------


@requires_provider
def test_missing_credential_raises_and_names_the_variable(no_credentials: Any) -> None:
    """The insurance policy against a poisoned control experiment.

    ``docs/ai-handoff.md`` is explicit: an empty ``AI_API_KEY`` "means unconfigured — a real
    provider should fail loudly at startup rather than degrade silently". If it degraded instead,
    ``make evaluate-ai`` would produce a number that looks like an AI result and is in fact the
    BM25 baseline with extra latency — and nothing in the response would say so.

    The message must name the variable because ``load_provider`` runs at ``app.main`` import
    time: the operator sees a traceback in the ai-adapter log and nothing else.
    """
    # No specific exception type is required: the provider may raise RuntimeError, ValueError or
    # its own class. What is required is that it raises at all, and says which variable is empty.
    with pytest.raises(Exception) as caught:
        build_provider()

    message = str(caught.value)
    named = [name for name in CREDENTIAL_VARS if name in message]
    assert named, (
        f"the error must name the missing environment variable so an operator can act on it; "
        f"got {message!r}. Expected one of {list(CREDENTIAL_VARS)} (AI_API_KEY is the documented "
        f"one)."
    )
    assert FAKE_KEY not in message


@requires_provider
def test_missing_credential_also_fails_through_load_provider(
    no_credentials: pytest.MonkeyPatch,
) -> None:
    """The failure has to survive the real entry point, not just direct construction."""
    no_credentials.setenv("AI_PROVIDER", PROVIDER_DOTTED_PATH)
    with pytest.raises(Exception):  # noqa: B017 - propagated from the provider's constructor
        load_provider()


@requires_provider
def test_constructor_takes_no_arguments() -> None:
    """``load_provider`` calls ``provider_class()`` with no arguments, at import time."""
    assert PROVIDER_CLASS is not None
    assert list(inspect.signature(PROVIDER_CLASS.__init__).parameters) == ["self"]


@requires_provider
def test_load_provider_returns_this_provider_when_configured(configured_env: Any) -> None:
    configured_env.setenv("AI_PROVIDER", PROVIDER_DOTTED_PATH)
    provider = load_provider()
    assert isinstance(provider, PROVIDER_CLASS)
    assert isinstance(provider, Provider)
    # A non-blank, non-"mock" name: the Java client forwards it as `ai_provider` and reports
    # "unknown" for a blank one, which would make the two evaluation runs indistinguishable.
    assert isinstance(provider.name, str)
    assert provider.name.strip()
    assert provider.name != "mock"


# ---------------------------------------------------------------------------
# 2. It really goes through LangChain
# ---------------------------------------------------------------------------


@requires_provider
def test_rewrite_calls_langchain_and_opens_no_socket(configured_env: Any, no_network: None) -> None:
    """Constructing and rewriting must both stay inside the (stubbed) LangChain surface."""
    BEHAVIOUR.result = llm_answer("wireless noise cancelling headphones")
    provider = build_provider()

    assert BEHAVIOUR.factory_calls, (
        "no LangChain chat model was constructed; the provider must build its model through "
        "LangChain (init_chat_model or a langchain_* chat class), not a hand-rolled HTTP client"
    )

    result, error = rewrite(provider, "wireless noise cancelling headphones")
    assert error is None, f"happy path raised {error!r}"
    assert BEHAVIOUR.invoke_calls, "the provider never invoked the LangChain model"
    assert result[0].strip()


@requires_provider
def test_prompt_carries_the_query_and_never_the_credential(configured_env: Any) -> None:
    """What is sent must contain the shopper's query and must not contain the key.

    ``explanation`` and adapter logs are both operator-visible; a prompt that embeds the
    credential eventually lands in one of them.
    """
    BEHAVIOUR.result = llm_answer("lawnmower tires without rims")
    provider = build_provider()
    _result, error = rewrite(provider, "!awnmower tires without rims")
    assert error is None

    sent = prompt_text()
    assert "awnmower tires without rims" in sent, (
        f"the model was invoked without the user's query in the prompt; sent: {sent!r}"
    )
    assert FAKE_KEY not in sent


# ---------------------------------------------------------------------------
# 3. Contract compliance
# ---------------------------------------------------------------------------


@requires_provider
def test_rewrite_returns_the_documented_tuple(configured_env: Any) -> None:
    """``app.main.query_rewrite`` unpacks exactly four values; a mismatch is a 500, not a 422."""
    BEHAVIOUR.result = llm_answer("laptop notebook computer", confidence=0.77)
    provider = build_provider()

    result, error = rewrite(provider, "laptop")
    assert error is None, f"happy path raised {error!r}"
    assert isinstance(result, tuple)
    assert len(result) == 4

    rewritten, filters, confidence, explanation = result
    assert isinstance(rewritten, str)
    assert rewritten.strip(), "a blank rewritten_query is INVALID_RESPONSE on the Java side"
    assert isinstance(filters, dict)
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0
    assert isinstance(explanation, str)
    assert FAKE_KEY not in explanation
    assert FAKE_KEY not in rewritten


@requires_provider
def test_rewrite_result_validates_as_the_response_model(configured_env: Any) -> None:
    """Build the exact object main.py builds; pydantic then enforces the wire contract."""
    BEHAVIOUR.result = llm_answer("usb c charging cable")
    provider = build_provider()
    result, error = rewrite(provider, "usb c cable")
    assert error is None
    rewritten, filters, confidence, explanation = result

    response = QueryRewriteResponse(
        original_query="usb c cable",
        rewritten_query=rewritten,
        extracted_filters=filters,
        confidence=confidence,
        explanation=explanation,
        provider=provider.name,
        latency_ms=0,
    )
    assert response.rewritten_query.strip()


@requires_provider
def test_filters_pass_through_without_mutating_the_request(configured_env: Any) -> None:
    BEHAVIOUR.result = llm_answer("running shoes")
    provider = build_provider()
    payload = request_for("running shoes", filters={"brand": "Demo", "price_max": 500})

    _rewritten, extracted, _confidence, _explanation = provider.rewrite(payload)
    assert isinstance(extracted, dict)
    extracted["injected"] = True
    assert payload.filters == {"brand": "Demo", "price_max": 500}, (
        "the provider returned the request's own filters dict; mutating the response then "
        "corrupts the request object"
    )


@requires_provider
def test_http_surface_reports_this_provider(configured_env: Any) -> None:
    """The body the Java client parses off the wire, field for field."""
    BEHAVIOUR.result = llm_answer("laptop bag shoulder")
    provider = build_provider()
    configured_env.setattr(main, "provider", provider)

    response = client.post(
        "/ai/query-rewrite",
        json={
            "query": "Laptop Bag",
            "locale": "en-US",
            "filters": {"brand": "Demo"},
            "request_id": "rewrite-langchain-1",
        },
    )
    assert response.status_code == 200, response.text
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
        # `model` is new: `provider` names the provider class, `model` names the model that
        # actually produced this answer. Listed explicitly so that a field appearing or
        # disappearing on the wire is always a deliberate edit to this line.
        "model",
    }
    assert body["provider"] == provider.name
    assert body["provider"] != "mock"
    assert body["original_query"] == "Laptop Bag"
    assert body["rewritten_query"].strip()
    assert body["latency_ms"] >= 0
    assert FAKE_KEY not in json.dumps(body)


# ---------------------------------------------------------------------------
# 4. Negation must survive
# ---------------------------------------------------------------------------

# Real ESCI queries from the evaluation set, noise and all.
NEGATION_QUERIES = [
    ("fence without holes", "fence panel without holes"),
    ("# 2 pencils not sharpened", "#2 pencils not sharpened"),
    ("!awnmower tires without rims", "lawnmower tires without rims"),
]


@requires_provider
@pytest.mark.parametrize(("query", "model_output"), NEGATION_QUERIES)
def test_negation_from_the_model_reaches_the_search_service(
    configured_env: Any, query: str, model_output: str
) -> None:
    """The provider's own post-processing must not strip the negation the model kept.

    Trimming, lowercasing and collapsing whitespace are fine. Stopword removal is not: "without"
    and "not" are stopwords by frequency and load-bearing by meaning. This tests the provider's
    handling of a stubbed answer, not the model's ability to produce one.
    """
    BEHAVIOUR.result = llm_answer(model_output)
    provider = build_provider()
    result, error = rewrite(provider, query)
    assert error is None, f"{query!r} raised {error!r}"

    rewritten = result[0]
    assert negation_survives(rewritten), (
        f"the model returned {model_output!r} but the provider emitted {rewritten!r}, which no "
        f"longer excludes anything; BM25 will now rank the excluded products highest"
    )


@requires_provider
@pytest.mark.parametrize(
    ("model_output", "how"),
    [
        ("fence holes panel", "the model simply deleted the negation word"),
        ("fence -holes", "the model encoded the negation as a BM25 operator"),
    ],
    ids=["deleted", "operator-encoded"],
)
def test_a_rewrite_that_drops_the_negation_is_not_forwarded(
    configured_env: Any, model_output: str, how: str
) -> None:
    """When the *model* loses the negation, the provider must not pass the result on.

    Either guard is acceptable — raise (the Java client records TRANSPORT_ERROR or
    INVALID_RESPONSE and falls back to BM25, visibly), or fall back to the original query. What
    is not acceptable is emitting "fence holes" for "fence without holes": that retrieves exactly
    the products the shopper excluded, and it scores as a normal relevance result, so the
    evaluation records a regression with no indication of where it came from.

    Both cases are observed behaviour, not hypotheticals. The second is the more insidious one:
    a model that answers "fence -holes" has kept the *intent* and only chosen the wrong notation,
    but stripping the operator — which a provider must do, since the compiled query is a
    ``multi_match`` that would match "-holes" as a literal token — silently converts an exclusion
    into an inclusion. Removing the operator and checking that the negation survived are two
    separate obligations, and doing only the first is worse than doing neither.
    """
    BEHAVIOUR.result = llm_answer(model_output)
    provider = build_provider()
    result, error = rewrite(provider, "fence without holes")
    if error is not None:
        return  # raised: loud, and the Java side reports a fallback status

    rewritten = result[0]
    assert negation_survives(rewritten), (
        f"{how}, and the provider emitted {rewritten!r} for 'fence without holes'. Nothing "
        f"downstream can tell this apart from a good rewrite: it is well-formed, it is non-blank, "
        f"and it retrieves the excluded products. Guard it — fall back to the original query, or "
        f"raise — instead of shipping an inverted query into the evaluation."
    )


# ---------------------------------------------------------------------------
# 5. Bad model output must never become a quiet bad rewrite
# ---------------------------------------------------------------------------


@requires_provider
@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_model_output_never_becomes_a_blank_rewritten_query(
    configured_env: Any, blank: str
) -> None:
    """A blank rewritten_query is the Java client's INVALID_RESPONSE, i.e. a silent BM25 run."""
    BEHAVIOUR.result = llm_answer(blank)
    provider = build_provider()
    result, error = rewrite(provider, "usb c cable")
    if error is not None:
        return  # raising is the loud option and is fine
    assert result[0].strip(), (
        "the provider passed the model's blank answer straight through; the search service will "
        "report INVALID_RESPONSE and fall back to BM25 while the adapter still answers 200"
    )


@requires_provider
def test_unparseable_model_output_does_not_leak_into_the_query(configured_env: Any) -> None:
    """Non-JSON text must not be handed to Elasticsearch as if it were a query."""
    junk = "<<<not-json-at-all>>> I'm sorry, I cannot help with that request."
    BEHAVIOUR.result = llm_raw(junk)
    provider = build_provider()
    result, error = rewrite(provider, "usb c cable")
    if error is not None:
        return
    assert "<<<not-json-at-all>>>" not in result[0], (
        f"the provider forwarded unparsed model prose as the query: {result[0]!r}"
    )
    assert result[0].strip()


@requires_provider
@pytest.mark.parametrize("confidence", [7.5, -1.0, 1.0000001])
def test_out_of_range_confidence_cannot_reach_the_response_model(
    configured_env: Any, confidence: float
) -> None:
    """``QueryRewriteResponse.confidence`` is ``ge=0, le=1``; a violation is a 500 from main.py.

    A 500 is at least loud, but it costs the whole request. Clamping or replacing is better; the
    only unacceptable outcome is returning an out-of-range float that blows up in FastAPI.
    """
    BEHAVIOUR.result = llm_answer("bluetooth speaker", confidence=confidence)
    provider = build_provider()
    result, error = rewrite(provider, "bluetooth speaker")
    if error is not None:
        return
    assert 0.0 <= result[2] <= 1.0, (
        f"confidence {result[2]!r} came straight from the model and fails pydantic validation "
        f"in app/main.py, turning a usable rewrite into an HTTP 500"
    )


# ---------------------------------------------------------------------------
# 6. Model failures must propagate, not be swallowed
# ---------------------------------------------------------------------------


@requires_provider
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
    configured_env: Any, failure: BaseException
) -> None:
    """Swallowing the error is the single most damaging thing this provider could do.

    If ``rewrite`` catches the failure and returns the original query, the adapter answers 200
    with a rewrite that never happened. The search service then records ``ai_status=NO_CHANGE``
    — a *success* state, ``fallback()`` false — and the evaluation attributes a pure-BM25 run to
    the AI path. Letting it escape gives main.py a 500, which the Java client maps to
    TRANSPORT_ERROR (or TIMEOUT if the 5000ms read budget went first): both are fallback states,
    both are visible in ``ai_status``, and neither can be mistaken for an AI result.
    """
    BEHAVIOUR.result = llm_answer("should never be returned")
    BEHAVIOUR.raises = failure
    provider = build_provider()

    result, error = rewrite(provider, "wireless keyboard")
    assert error is not None, (
        f"rewrite swallowed {type(failure).__name__} and returned {result!r}; the search service "
        f"would record this as a successful AI rewrite that never reached a model"
    )


# ---------------------------------------------------------------------------
# 7. Configuration: documented defaults, and overrides that actually arrive
# ---------------------------------------------------------------------------


@requires_provider
def test_configuration_defaults_when_nothing_is_set(configured_env: Any) -> None:
    """With only a credential set, the documented defaults must apply.

    ``AI_TEMPERATURE=0`` is not cosmetic: ``.env.example`` ties it to reproducible evaluation,
    and the archived BM25 baseline is only comparable to an AI run if the AI run is repeatable.
    """
    build_provider()
    assert BEHAVIOUR.factory_calls, "no chat model was constructed"

    kwargs = recorded_kwargs()
    assert kwargs, "the provider passed no configuration to the chat model at all"

    if "temperature" in kwargs:
        assert float(kwargs["temperature"]) == 0.0, (
            f"default temperature must be 0 for reproducible evaluation, got "
            f"{kwargs['temperature']!r}"
        )
    else:
        numeric = [v for v in recorded_arguments() if isinstance(v, (int, float))]
        numeric = [v for v in numeric if not isinstance(v, bool)]
        assert any(float(v) == 0.0 for v in numeric), (
            f"no zero temperature reached the chat model; recorded numbers: {numeric!r}"
        )

    model_values = [str(v) for v in recorded_arguments() if isinstance(v, str) and v.strip()]
    assert model_values, "the provider did not name a model; AI_MODEL has no default in .env"


@requires_provider
def test_configuration_overrides_reach_the_chat_model(configured_env: Any) -> None:
    """Every documented knob must be observable on the LangChain call.

    A knob that is read from the environment and then not forwarded is worse than one that is
    not read at all: `.env` says temperature 0 while the model samples at its own default, and
    the evaluation is quietly irreproducible.
    """
    # Both values must differ from the ones `configured_env` already set, otherwise the
    # assertions below would pass just as happily against a provider that ignores the override.
    configured_env.setenv("AI_MODEL", "qwen3.7-flash-2026-07-15")
    configured_env.setenv("AI_API_BASE_URL", "https://override.invalid/v1")
    configured_env.setenv("AI_TEMPERATURE", "0.35")
    configured_env.setenv("AI_MAX_TOKENS", "77")
    configured_env.setenv("AI_REQUEST_TIMEOUT_MS", "2500")

    build_provider()
    assert BEHAVIOUR.factory_calls, "no chat model was constructed"

    assert mentions("qwen3.7-flash-2026-07-15"), (
        f"AI_MODEL never reached the chat model; recorded: {recorded_arguments()!r}"
    )
    assert mentions("override.invalid"), (
        f"AI_API_BASE_URL never reached the chat model; recorded: {recorded_arguments()!r}"
    )
    assert mentions("0.35"), (
        f"AI_TEMPERATURE never reached the chat model; recorded: {recorded_arguments()!r}"
    )
    assert mentions("77"), (
        f"AI_MAX_TOKENS never reached the chat model; recorded: {recorded_arguments()!r}"
    )
    # 2500ms may legitimately be forwarded as seconds; accept either spelling.
    assert mentions("2500") or mentions("2.5"), (
        f"AI_REQUEST_TIMEOUT_MS never reached the chat model. It must stay below AI_TIMEOUT_MS "
        f"(5000ms) or Java gives up while the adapter is still waiting. Recorded: "
        f"{recorded_arguments()!r}"
    )


# ---------------------------------------------------------------------------
# 8. Regression guard
# ---------------------------------------------------------------------------


def test_default_provider_is_untouched_by_this_module() -> None:
    """Nothing here may leave ``app.main`` pointing at the LangChain provider.

    ``scripts/test-unit.sh`` does not source ``.env``, so the default path must stay keyless,
    offline and deterministic for every other test in the suite.
    """
    assert main.provider.name == "mock"
