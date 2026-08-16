#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

UPSERT = """
INSERT INTO lab_products
  (product_id, title, brand, price_cents, currency, inventory, provenance, updated_at)
VALUES (%(product_id)s, %(title)s, %(brand)s, %(price_cents)s, %(currency)s,
        %(inventory)s, %(provenance)s, now())
ON CONFLICT (product_id) DO UPDATE SET
  title = EXCLUDED.title,
  brand = EXCLUDED.brand,
  price_cents = EXCLUDED.price_cents,
  currency = EXCLUDED.currency,
  inventory = EXCLUDED.inventory,
  provenance = EXCLUDED.provenance,
  updated_at = now()
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", type=Path, default=Path(__file__).resolve().parents[1] / "processed" / "products.jsonl")
    parser.add_argument("--database-url", default=os.getenv("COMMERCE_DATABASE_URL", "postgresql://searchops:searchops_local_only@localhost:5432/commerce"))
    args = parser.parse_args()
    if not args.products.exists():
        raise FileNotFoundError("Processed products are missing; run make data first")
    count = 0
    batch: list[dict] = []
    with psycopg.connect(args.database_url) as connection:
        with connection.cursor() as cursor, args.products.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                batch.append(json.loads(line))
                if len(batch) >= 1000:
                    cursor.executemany(UPSERT, batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                cursor.executemany(UPSERT, batch)
                count += len(batch)
        connection.commit()
        actual = connection.execute("SELECT count(*) FROM lab_products").fetchone()[0]
    print(json.dumps({"status": "ok", "upserted": count, "commerce_product_count": actual, "idempotent": True}))


if __name__ == "__main__":
    main()

