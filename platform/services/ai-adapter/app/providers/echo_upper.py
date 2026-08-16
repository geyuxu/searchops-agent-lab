"""A keyless, deterministic provider whose only purpose is to prove the extension point works.

This is *not* a relevance feature and *not* the LangChain provider. It exists to answer one
question end to end: does a provider whose name is not ``"mock"`` actually reach Elasticsearch
and change what the search service returns? Until this provider runs green, the claim in
``docs/ai-handoff.md`` that a real AI service can be dropped in without touching the storefront,
the search backend or the frontend is unverified.

Expect evaluation metrics to *move*, quite possibly downwards. Appending a marker token is a
deliberate perturbation of BM25 scoring, not an improvement. Never read a ``use_ai=true``
evaluation run against this provider as an "AI relevance result" — read it only as proof that
the AI path is wired up and observable.
"""

from __future__ import annotations

from typing import ClassVar

from ..models import (
    ProposedChange,
    QueryRewriteRequest,
    RerankRequest,
    RerankScore,
    StrategySuggestRequest,
)
from ..provider import MockProvider, Provider


class EchoUpperProvider(Provider):
    """Normalizes the query and appends one fixed marker token. Nothing else."""

    name = "echo-upper"

    #: Appended to every rewritten query. See ``rewrite`` for why this exact word.
    MARKER: ClassVar[str] = "product"

    def __init__(self) -> None:
        # Zero-argument and I/O-free on purpose: app/main.py calls load_provider() at import
        # time, so anything raised here is a container that never becomes healthy, and
        # depends_on: service_healthy then blocks the rest of the stack from starting.
        # Constructing MockProvider is a plain object allocation and cannot fail.
        self._delegate = MockProvider()

    @staticmethod
    def _normalize(value: str) -> str:
        """Trim, collapse runs of whitespace, lowercase.

        Deliberately identical to ``SearchQueryCompiler.applyRewrite`` and to
        ``AiAdapterClient.normalize`` on the Java side. Matching it here means the only
        difference between the original and the rewritten query is the marker token, so
        ``ai_applied`` / ``ai_status`` flip for exactly one visible, explainable reason.
        """
        return " ".join(value.split()).lower()

    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]:
        """Return ``"<normalized query> product"``.

        Why append a token instead of upper-casing, despite the provider's name: the search
        service lowercases the rewritten query before comparing it to the original, so a
        case-only rewrite is byte-identical after normalization and reports
        ``ai_status=NO_CHANGE`` / ``ai_applied=false`` — precisely the outcome this provider
        exists to disprove. The transformation must survive lowercasing, so it has to add or
        remove a token.

        Why a real corpus word rather than an invented sentinel such as ``"echoupper"``: the
        compiled Elasticsearch query is ``multi_match`` with ``operator: or``, so a token that
        matches no document contributes nothing to BM25 and the hit list comes back byte
        identical to the pure-BM25 baseline. That would prove the JSON travelled, but nothing
        about the retrieval path. ``product`` appears in 3,191 of the 20,000 indexed ESCI
        products (16.0% of the corpus, measured over title/brand/bullet_point/description/
        category), so scores genuinely move.

        Why a *common* word rather than a rare one: under ``operator: or`` a high-IDF marker
        would dominate every score and effectively turn every query into a search for the
        marker itself. A ~16% document frequency perturbs the ranking without hijacking it.
        It is also plain ASCII English, so it cannot produce mojibake and it stays inside the
        US-English ESCI subset.

        Why the marker is appended unconditionally, even when the query already contains it:
        an unconditional append is the only version with no input that maps to itself. The
        postcondition — ``normalize(rewritten) != normalize(original)`` for every request the
        schema accepts — then holds for repeated markers and for whitespace-only queries
        (``models.QueryRewriteRequest`` enforces ``min_length=1``, which " " satisfies). It
        also guarantees a non-blank ``rewritten_query``; a blank one makes the search service
        report ``ai_status=INVALID_RESPONSE`` and fall back to BM25.

        Recall is not at risk: adding a clause to an ``or`` query can only add matching
        documents, so this rewrite cannot introduce a zero-result query. Only the ordering
        changes.
        """
        rewritten = f"{self._normalize(payload.query)} {self.MARKER}".strip()
        return (
            # Filters are copied through untouched. Extracting filters is explicitly out of
            # scope for this increment (the search service ignores extracted_filters today),
            # and inventing them here would confuse a later comparison against a provider
            # that does extract them.
            rewritten,
            dict(payload.filters),
            # Constant, because the transformation is deterministic. This is confidence in
            # the mechanics, not a relevance judgement — the provider makes none.
            1.0,
            (
                f"Verification provider: normalized the query and appended the fixed corpus "
                f"marker '{self.MARKER}'. Deterministic, keyless, and intended only to prove "
                f"that a non-mock provider reaches Elasticsearch; it makes no relevance claim."
            ),
        )

    # rerank/suggest are out of scope for this increment, which is about the query-rewrite
    # path only. Delegating to MockProvider keeps those two endpoints behaving exactly as they
    # do in the default deployment, so switching AI_PROVIDER to this class changes one thing
    # and one thing only.

    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]:
        """Delegate verbatim to :class:`~app.provider.MockProvider`."""
        return self._delegate.rerank(payload)

    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]:
        """Delegate verbatim to :class:`~app.provider.MockProvider`."""
        return self._delegate.suggest(payload)
