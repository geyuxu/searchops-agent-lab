from __future__ import annotations

import hashlib
import importlib
import os
import re
from abc import ABC, abstractmethod
from typing import ClassVar

from .models import (
    ProposedChange,
    QueryRewriteRequest,
    RerankRequest,
    RerankScore,
    StrategySuggestRequest,
)


class Provider(ABC):
    name: str

    @abstractmethod
    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]: ...

    @abstractmethod
    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]: ...

    @abstractmethod
    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]: ...


class MockProvider(Provider):
    """Deterministic, transparent provider used without any model or network call."""

    name = "mock"
    _expansions: ClassVar[dict[str, str]] = {
        "tv": "television",
        "laptop": "notebook computer",
        "headphones": "headset",
        "sneakers": "athletic shoes",
        "cellphone": "mobile phone",
    }

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.lower()))

    def rewrite(self, payload: QueryRewriteRequest) -> tuple[str, dict, float, str]:
        normalized = " ".join(payload.query.strip().lower().split())
        filters = dict(payload.filters)
        price_match = re.search(
            r"(?:under|below|less than)\s*\$?\s*(\d+(?:\.\d{1,2})?)",
            normalized,
        )
        if price_match:
            filters["price_lte"] = float(price_match.group(1))
            normalized = re.sub(price_match.re, "", normalized).strip()

        replacements: list[str] = []
        words: list[str] = []
        for word in normalized.split():
            words.append(word)
            if word in self._expansions:
                words.extend(self._expansions[word].split())
                replacements.append(f"{word}→{self._expansions[word]}")

        rewritten = " ".join(words) or payload.query.strip()
        if replacements or price_match:
            detail = ", ".join(replacements) if replacements else "price phrase extracted"
            return rewritten, filters, 0.82, f"Deterministic lexical rule applied: {detail}."
        return (
            rewritten,
            filters,
            0.55,
            "No deterministic rewrite rule matched; query normalized only.",
        )

    def rerank(self, payload: RerankRequest) -> tuple[list[str], list[RerankScore], str]:
        query_tokens = self._tokens(payload.query)
        ranked: list[tuple[float, str]] = []
        for candidate in payload.candidates:
            text_tokens = self._tokens(
                f"{candidate.title} {candidate.brand} {candidate.description}"
            )
            overlap = len(query_tokens & text_tokens) / max(len(query_tokens), 1)
            lexical = overlap * 0.8
            baseline = max(candidate.bm25_score, 0) / (max(candidate.bm25_score, 0) + 10) * 0.19
            stable = int(hashlib.sha256(candidate.product_id.encode()).hexdigest()[:6], 16)
            score = round(lexical + baseline + stable / 0xFFFFFF * 0.01, 6)
            ranked.append((score, candidate.product_id))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        scores = [RerankScore(product_id=product_id, score=score) for score, product_id in ranked]
        return (
            [item.product_id for item in scores],
            scores,
            "Deterministic lexical overlap (80%), bounded BM25 (19%), and stable tie-break (1%).",
        )

    def suggest(
        self, payload: StrategySuggestRequest
    ) -> tuple[list[ProposedChange], str, list[str], float, str]:
        changes: list[ProposedChange] = []
        refs: list[str] = []
        for index, metric in enumerate(payload.query_metrics):
            query = str(metric.get("query", "")).strip()
            zero = bool(metric.get("zero_result", False))
            ndcg = float(metric.get("ndcg10", 1) or 0)
            if query and zero:
                changes.append(
                    ProposedChange(
                        operation="add",
                        path=f"/rewrite_rules/{len(changes)}",
                        value={"match": query, "rewrite": query},
                        reason=(
                            "Flagged for operator-authored rewrite because the query "
                            "returned no results."
                        ),
                    )
                )
                refs.append(f"query_metrics[{index}]")
            elif query and ndcg < 0.35:
                changes.append(
                    ProposedChange(
                        operation="review",
                        path="/field_weights/title",
                        value=3.0,
                        reason=(
                            f"Low NDCG@10 ({ndcg:.3f}) suggests testing stronger "
                            "title weighting."
                        ),
                    )
                )
                refs.append(f"query_metrics[{index}]")
            if len(changes) >= 5:
                break

        if not changes:
            return (
                [],
                "No deterministic risk signal crossed the suggestion thresholds.",
                [],
                0.6,
                "low",
            )
        risk = "medium" if any(c.operation == "add" for c in changes) else "low"
        return (
            changes,
            "Preview these bounded changes against retained ESCI judgments.",
            refs,
            0.72,
            risk,
        )


def load_provider() -> Provider:
    configured = os.getenv("AI_PROVIDER", "mock").strip()
    if configured == "mock":
        return MockProvider()
    if ":" not in configured:
        raise RuntimeError("AI_PROVIDER must be 'mock' or a 'module.path:ClassName' provider")
    module_name, class_name = configured.split(":", 1)
    provider_class = getattr(importlib.import_module(module_name), class_name)
    provider = provider_class()
    if not isinstance(provider, Provider):
        raise TypeError("Configured provider must implement app.provider.Provider")
    return provider
