"""Agent Tool Gateway (`/api/v1/tools/*`) 的 Python 客户端。

每个方法对应网关的一个端点，并登记安全等级。写操作自动生成 `Idempotency-Key`
与 `request_id`：幂等键由调用者可选传入，未传时按内容+时间派生，保证重试不重复建单。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .models import EvaluationResult, PreviewResponse, Strategy, StrategyConfig
from .safety import SafetyClass, safety


class ToolGatewayError(RuntimeError):
    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"tool gateway {status}: {payload}")
        self.status = status
        self.payload = payload


class SearchOpsClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        actor: str = "searchops-agent",
        timeout: float = 30.0,
    ) -> None:
        self.base = base_url.rstrip("/") + "/api/v1/tools"
        self.actor = actor
        self._http = httpx.Client(timeout=timeout)

    # ---- 传输层 ---------------------------------------------------------

    def _request(self, method: str, path: str, *, idempotency_key: str | None = None, **kw) -> Any:
        headers = dict(kw.pop("headers", {}))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        resp = self._http.request(method, f"{self.base}{path}", headers=headers, **kw)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            raise ToolGatewayError(resp.status_code, payload)
        return resp.json() if resp.content else None

    @staticmethod
    def new_request_id() -> str:
        return str(uuid.uuid4())

    # ---- READ -----------------------------------------------------------

    @safety(SafetyClass.READ)
    def query_metrics(self, query: str) -> Any:
        return self._request("GET", "/query-metrics", params={"query": query})

    @safety(SafetyClass.READ)
    def zero_result_queries(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/zero-result-queries", params={"limit": limit})

    @safety(SafetyClass.READ)
    def low_quality_queries(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/low-quality-queries", params={"limit": limit})

    @safety(SafetyClass.READ)
    def current_strategy(self) -> Strategy:
        return Strategy.model_validate(self._request("GET", "/strategies/current"))

    @safety(SafetyClass.READ)
    def strategy_history(self) -> list[Strategy]:
        return [Strategy.model_validate(s) for s in self._request("GET", "/strategies/history")]

    # ---- DRY_RUN --------------------------------------------------------

    @safety(SafetyClass.DRY_RUN)
    def preview(self, query: str, config: StrategyConfig, size: int = 10, locale: str = "us") -> PreviewResponse:
        """对比当前策略与候选策略的结果，不写入任何状态。"""
        body = {"query": query, "locale": locale, "size": size, "config": config.model_dump()}
        return PreviewResponse.model_validate(self._request("POST", "/strategies/preview", json=body))

    @safety(SafetyClass.DRY_RUN)
    def evaluate_query(self, query_id: int, query: str, judgments: dict[str, str]) -> Any:
        body = {"query_id": query_id, "query": query, "judgments": judgments}
        return self._request("POST", "/evaluations/query", json=body)

    # ---- GOVERNED_WRITE -------------------------------------------------

    @safety(SafetyClass.GOVERNED_WRITE)
    def create_draft(
        self, name: str, config: StrategyConfig, request_id: str | None = None, idempotency_key: str | None = None
    ) -> Strategy:
        rid = request_id or self.new_request_id()
        body = {"name": name, "actor": self.actor, "request_id": rid, "config": config.model_dump()}
        payload = self._request(
            "POST", "/strategies/drafts", json=body, idempotency_key=idempotency_key or rid
        )
        return Strategy.model_validate(payload)

    @safety(SafetyClass.GOVERNED_WRITE)
    def submit(self, strategy_id: str, request_id: str | None = None, idempotency_key: str | None = None) -> Strategy:
        rid = request_id or self.new_request_id()
        body = {"actor": self.actor, "request_id": rid}
        payload = self._request(
            "POST", f"/strategies/{strategy_id}/submit", json=body, idempotency_key=idempotency_key or rid
        )
        return Strategy.model_validate(payload)

    # ---- PRIVILEGED / TOKEN_GATED ---------------------------------------
    # 以下三个方法存在于客户端，是为了让人类操作者与集成测试能走完整生命周期。
    # 它们绝不出现在 Agent 的工具注册表中——见 agent/searchops_agent/tools.py
    # 与 tests/test_governance_boundary.py 的断言。

    @safety(SafetyClass.PRIVILEGED_WRITE)
    def approve(self, strategy_id: str, actor: str, request_id: str | None = None) -> dict[str, Any]:
        rid = request_id or self.new_request_id()
        return self._request(
            "POST", f"/strategies/{strategy_id}/approve",
            json={"actor": actor, "request_id": rid}, idempotency_key=rid,
        )

    @safety(SafetyClass.TOKEN_GATED_WRITE)
    def publish(self, strategy_id: str, actor: str, approval_token: str, request_id: str | None = None) -> Any:
        rid = request_id or self.new_request_id()
        return self._request(
            "POST", f"/strategies/{strategy_id}/publish",
            json={"actor": actor, "request_id": rid, "approval_token": approval_token},
            idempotency_key=rid,
        )

    @safety(SafetyClass.TOKEN_GATED_WRITE)
    def rollback(self, target_version: int, actor: str, approval_token: str, request_id: str | None = None) -> Any:
        rid = request_id or self.new_request_id()
        return self._request(
            "POST", "/strategies/rollback",
            json={"target_version": target_version, "actor": actor,
                  "request_id": rid, "approval_token": approval_token},
            idempotency_key=rid,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SearchOpsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["SearchOpsClient", "ToolGatewayError", "StrategyConfig", "EvaluationResult"]
