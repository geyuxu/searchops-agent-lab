#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:8080/api/v1"


def request(path: str, *, payload: dict | None = None, key: str | None = None, request_id: str) -> dict | list:
    headers = {"X-Request-ID": request_id}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=60) as response:
        return json.load(response)


def search(query: str, request_id: str) -> dict:
    return request("/search?" + urllib.parse.urlencode({"q": query, "size": 10}), request_id=request_id)


def main() -> None:
    query_record = json.loads(next(line for line in (ROOT / "data/processed/queries.jsonl").open() if line.strip()))
    product_lines = (ROOT / "data/processed/products.jsonl").open()
    candidates = [json.loads(next(product_lines))["product_id"] for _ in range(25)]
    query = query_record["query"]

    before_strategy = request("/strategies/current", request_id="policy-current-before")
    before_results = search(query, "policy-search-before")
    before_ids = [item["product_id"] for item in before_results["products"]]
    pinned = next(product_id for product_id in candidates if product_id not in before_ids[:1])
    config = dict(before_strategy["config"])
    config["pinned_product_ids"] = [pinned]

    create_key = f"integration-create-from-{before_strategy['version']}"
    create_payload = {
        "name": "Integration pinned-result policy",
        "actor": "integration-test",
        "request_id": "policy-create",
        "config": config,
    }
    draft = request("/strategies", payload=create_payload, key=create_key, request_id="policy-create")
    replay = request("/strategies", payload=create_payload, key=create_key, request_id="policy-create-replay")
    assert replay["id"] == draft["id"], "Idempotent create did not replay the original result"

    submitted = request(
        f"/strategies/{draft['id']}/submit",
        payload={"actor": "integration-test", "request_id": "policy-submit"},
        key=f"integration-submit-{draft['id']}", request_id="policy-submit",
    )
    assert submitted["status"] == "IN_REVIEW"
    approved = request(
        f"/strategies/{draft['id']}/approve",
        payload={"actor": "approver-test", "request_id": "policy-approve"},
        key=f"integration-approve-{draft['id']}", request_id="policy-approve",
    )
    token = approved["approval_token"]
    published = request(
        f"/strategies/{draft['id']}/publish",
        payload={"actor": "integration-test", "request_id": "policy-publish", "approval_token": token},
        key=f"integration-publish-{draft['id']}", request_id="policy-publish",
    )
    assert published["status"] == "PUBLISHED"

    after_ids = [item["product_id"] for item in search(query, "policy-search-after")["products"]]
    assert after_ids[0] == pinned, "Published pin did not change the Elasticsearch result order"

    rolled = request(
        "/strategies/rollback",
        payload={"target_version": before_strategy["version"], "actor": "integration-test", "request_id": "policy-rollback", "approval_token": token},
        key=f"integration-rollback-{draft['id']}", request_id="policy-rollback",
    )
    assert rolled["status"] == "PUBLISHED"
    restored_ids = [item["product_id"] for item in search(query, "policy-search-restored")["products"]]
    assert restored_ids[:5] == before_ids[:5], "Rollback did not restore the prior ranking"

    audits = request("/audit?limit=50", request_id="policy-audit")
    actions = {row["action"] for row in audits}
    assert {"STRATEGY_CREATE", "STRATEGY_SUBMIT", "STRATEGY_APPROVE", "STRATEGY_PUBLISH", "STRATEGY_ROLLBACK"}.issubset(actions)
    print(json.dumps({
        "status": "ok",
        "query": query,
        "pinned_product_id": pinned,
        "published_version": published["version"],
        "rollback_version": rolled["version"],
        "ranking_changed": True,
        "ranking_restored": True,
        "audited_actions": sorted(actions),
    }, indent=2))


if __name__ == "__main__":
    main()
