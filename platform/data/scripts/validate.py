#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--processed-dir", type=Path, default=Path(__file__).resolve().parents[1] / "processed")
    args = parser.parse_args()
    manifest = json.loads((args.processed_dir / "manifest.json").read_text())
    for name, expected in manifest["outputs"].items():
        path = args.processed_dir / name
        if digest(path) != expected:
            raise ValueError(f"Processed hash mismatch: {name}")
    products = [json.loads(line) for line in (args.processed_dir / "products.jsonl").open() if line.strip()]
    queries = [json.loads(line) for line in (args.processed_dir / "queries.jsonl").open() if line.strip()]
    assert len(products) == manifest["product_count"]
    assert len(queries) == manifest["query_count"]
    assert len({product["product_id"] for product in products}) == len(products)
    product_ids = {product["product_id"] for product in products}
    assert all(query["judgments"] for query in queries)
    assert all(set(query["judgments"]).issubset(product_ids) for query in queries)
    assert all(set(query["judgments"].values()).issubset({"E", "S", "C", "I"}) for query in queries)
    print(json.dumps({"status": "ok", "products": len(products), "queries": len(queries), "manifest_verified": True}))


if __name__ == "__main__":
    main()

