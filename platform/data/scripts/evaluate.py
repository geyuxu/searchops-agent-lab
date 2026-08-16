#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=Path(__file__).resolve().parents[1] / "processed" / "queries.jsonl")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "processed" / "evaluation-latest.json")
    parser.add_argument("--search-url", default="http://localhost:8080")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--persist", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    queries = []
    with args.queries.open(encoding="utf-8") as source:
        for line in source:
            if len(queries) >= args.limit:
                break
            item = json.loads(line)
            queries.append({"query_id": item["query_id"], "query": item["query"], "judgments": item["judgments"]})
    payload = json.dumps({"queries": queries, "k": 10, "persist": args.persist}).encode()
    request = urllib.request.Request(
        f"{args.search_url}/api/v1/evaluations/run",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Request-ID": "offline-evaluation"},
    )
    with urllib.request.urlopen(request, timeout=max(120, len(queries) * 3)) as response:
        result = json.load(response)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output.parent, delete=False) as output:
        json.dump(result, output, indent=2)
        output.write("\n")
        temporary = Path(output.name)
    temporary.replace(args.output)
    summary = {key: result[key] for key in ("run_id", "strategy_version", "query_count", "precision10", "recall10", "mrr10", "ndcg10", "zero_result_rate", "generated_at")}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

