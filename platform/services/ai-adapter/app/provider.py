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

    #: 本 provider 实际使用的模型标识, 原样填进各响应的 ``model`` 字段。
    #:
    #: 为什么是一个带默认值的类属性, 而不是加进抽象方法的返回元组: 返回元组的形状是
    #: ``load_provider`` 之外所有第三方 provider 都实现了的契约, 改它等于让每一个既有实现
    #: (含 echo_upper) 当场不可用。类属性对既有子类完全不可见 —— 它们自动继承 None, 含义是
    #: "这个 provider 没有声明模型标识"。
    #:
    #: None **不表示**"忘了记录"。凡是真的跑了模型的 provider 都必须在 ``__init__`` 里把它设成
    #: 实际配置的模型名; 凡是根本不跑模型的 provider 应当设一个明确的非模型标识
    #: (MockProvider 用 ``deterministic-mock``), 好让产物里"没有模型"与"漏记了模型"分得开。
    model: str | None = None

    def model_for(self, capability: str) -> str | None:
        """这条能力 (``rewrite`` / ``rerank`` / ``suggest``) 实际由哪个模型跑出来。

        默认三条能力回报同一个 ``model`` —— 对绝大多数 provider 这就是事实。

        存在这一层的唯一理由是 **委托**: 本仓库的两个 LangChain provider 都只实现一条能力,
        另外两条逐字委托给 ``MockProvider`` (刻意的, 见它们的类注释: 一次增量只改一件事)。
        若模型标识是一个扁平字段, 一个跑着 ``langchain-rerank`` 的适配器在
        ``/ai/query-rewrite`` 上会回报 ``model=qwen3.7-flash-...``, 而那次改写压根是确定性
        mock 做的 —— 这正是本次要修的那类缺陷 (产物声称的模型不是真正跑出结果的模型), 只是换了
        个位置重新长出来。委托了能力就必须连模型身份一起委托。

        ``capability`` 用裸字符串而不是枚举: 它是 ``app.main`` 三条路由各自传的常量, 第三方
        provider 完全可以忽略它 (默认实现就忽略), 引入一个必须 import 的枚举只会让外部实现多
        一处耦合, 换不来任何校验价值。
        """
        return self.model

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

    #: 一个**明确的非模型标识**, 不是留空。
    #:
    #: 留 None 会让评测产物里"这一轮压根没有模型参与"与"这一轮有模型但没人记下来"长得一模一样,
    #: 而这两件事对一份结论的可信度影响完全相反: 前者说明这个数字就是确定性基线, 后者说明这个
    #: 数字不可复现。填一个不可能被误认成厂商模型名的字符串, 是让产物在两种情况下自己说清楚。
    #: 取值刻意不叫 "mock" —— 那与 ``provider`` 字段重复, 读产物的人会以为是复制粘贴的冗余;
    #: "deterministic-mock" 同时说出了"确定性"这个真正重要的性质。
    model = "deterministic-mock"

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
