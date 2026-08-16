#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=Path(__file__).resolve().parents[1] / "processed" / "queries.jsonl")
    parser.add_argument("--search-url", default="http://localhost:8080")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    sent = 0
    with args.queries.open(encoding="utf-8") as source:
        for line in source:
            if sent >= args.limit:
                break
            query = json.loads(line)
            url = f"{args.search_url}/api/v1/search?{urllib.parse.urlencode({'q': query['query'], 'size': 10})}"
            request = urllib.request.Request(url, headers={"X-Request-ID": f"simulated-traffic-{query['query_id']}"})
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"Traffic request failed: {response.status}")
            sent += 1
    print(json.dumps({"status": "ok", "simulated_requests": sent, "idempotent_request_ids": True}))


if __name__ == "__main__":
    main()

