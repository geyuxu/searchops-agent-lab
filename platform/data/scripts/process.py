#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def parquet_ok(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Run the download step first.")
    with path.open("rb") as source:
        if source.read(4) != b"PAR1":
            raise ValueError(f"Not a parquet file: {path}")


def sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=root / "raw")
    parser.add_argument("--output-dir", type=Path, default=root / "processed")
    parser.add_argument("--product-limit", type=int, default=20_000)
    parser.add_argument("--query-limit", type=int, default=10_000)
    parser.add_argument("--seed", default="searchops-lab-v1")
    args = parser.parse_args()
    if args.product_limit < args.query_limit:
        raise ValueError("PRODUCT_LIMIT must be at least QUERY_LIMIT to retain one judgment per query")

    products_source = args.raw_dir / "shopping_queries_dataset_products.parquet"
    examples_source = args.raw_dir / "shopping_queries_dataset_examples.parquet"
    parquet_ok(products_source)
    parquet_ok(examples_source)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    products_output = args.output_dir / "products.jsonl"
    queries_output = args.output_dir / "queries.jsonl"

    connection = duckdb.connect(database=":memory:")
    connection.execute("SET threads = 4")
    connection.execute("SET memory_limit = '2GB'")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(
        f"""
        CREATE TEMP TABLE selected_queries AS
        SELECT query_id, min(query) AS query
        FROM read_parquet('{sql_path(examples_source)}')
        WHERE product_locale = 'us' AND small_version = 1
        GROUP BY query_id
        ORDER BY sha256(CAST(query_id AS VARCHAR) || '{args.seed.replace("'", "''")}')
        LIMIT {args.query_limit}
        """
    )
    actual_queries = connection.execute("SELECT count(*) FROM selected_queries").fetchone()[0]
    if actual_queries != args.query_limit:
        raise ValueError(f"Requested {args.query_limit} queries but only found {actual_queries}")

    connection.execute(
        f"""
        CREATE TEMP TABLE ranked_judgments AS
        SELECT e.query_id, q.query, e.product_id, e.esci_label, e.split,
               row_number() OVER (
                 PARTITION BY e.query_id
                 ORDER BY CASE e.esci_label WHEN 'E' THEN 0 WHEN 'S' THEN 1 WHEN 'C' THEN 2 ELSE 3 END,
                          sha256(e.product_id || '{args.seed.replace("'", "''")}')
               ) AS query_rank
        FROM read_parquet('{sql_path(examples_source)}') e
        JOIN selected_queries q USING (query_id)
        WHERE e.product_locale = 'us' AND e.small_version = 1
        """
    )
    connection.execute("CREATE TEMP TABLE mandatory_products AS SELECT DISTINCT product_id FROM ranked_judgments WHERE query_rank = 1")
    mandatory = connection.execute("SELECT count(*) FROM mandatory_products").fetchone()[0]
    remaining = args.product_limit - mandatory
    connection.execute(
        f"""
        CREATE TEMP TABLE selected_products AS
        SELECT product_id FROM mandatory_products
        UNION
        SELECT product_id FROM (
          SELECT DISTINCT r.product_id
          FROM ranked_judgments r
          LEFT JOIN mandatory_products m USING (product_id)
          WHERE m.product_id IS NULL
          ORDER BY sha256(r.product_id || '{args.seed.replace("'", "''")}')
          LIMIT {remaining}
        )
        """
    )
    selected = connection.execute("SELECT count(*) FROM selected_products").fetchone()[0]
    fill = args.product_limit - selected
    if fill > 0:
        connection.execute(
            f"""
            INSERT INTO selected_products
            SELECT product_id FROM (
              SELECT p.product_id
              FROM read_parquet('{sql_path(products_source)}') p
              LEFT JOIN selected_products s USING (product_id)
              WHERE p.product_locale = 'us' AND s.product_id IS NULL
              ORDER BY sha256(p.product_id || '{args.seed.replace("'", "''")}')
              LIMIT {fill}
            )
            """
        )

    connection.execute(
        f"""
        COPY (
          SELECT
            p.product_id,
            trim(COALESCE(CAST(p.product_title AS VARCHAR), 'Untitled product')) AS title,
            trim(COALESCE(CAST(p.product_brand AS VARCHAR), '')) AS brand,
            trim(COALESCE(CAST(p.product_description AS VARCHAR), '')) AS description,
            trim(COALESCE(CAST(p.product_bullet_point AS VARCHAR), '')) AS bullet_point,
            trim(COALESCE(CAST(p.product_color AS VARCHAR), '')) AS color,
            p.product_locale AS locale,
            CASE
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(shoe|sneaker|boot|sandal)%' THEN 'Footwear'
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(phone|laptop|computer|headphone|camera|tablet|charger)%' THEN 'Electronics'
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(shirt|dress|jacket|jean|sock|hat)%' THEN 'Apparel'
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(kitchen|mug|pan|lamp|chair|table|bedding)%' THEN 'Home'
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(toy|game|puzzle)%' THEN 'Toys & Games'
              WHEN lower(CAST(p.product_title AS VARCHAR)) SIMILAR TO '%(beauty|cream|shampoo|makeup|lotion)%' THEN 'Beauty'
              ELSE 'Other'
            END AS category,
            499 + CAST(hash(p.product_id || '{args.seed.replace("'", "''")}' || 'price') % 49500 AS INTEGER) AS price_cents,
            'USD' AS currency,
            CAST(hash(p.product_id || '{args.seed.replace("'", "''")}' || 'stock') % 81 AS INTEGER) AS inventory,
            CAST(hash(p.product_id || '{args.seed.replace("'", "''")}' || 'hue') % 360 AS INTEGER) AS placeholder_hue,
            CAST(hash(p.product_id || '{args.seed.replace("'", "''")}' || 'pop') % 100000 AS DOUBLE) / 100000.0 AS popularity,
            'Amazon Shopping Queries Dataset (ESCI), public product text' AS provenance
          FROM read_parquet('{sql_path(products_source)}') p
          JOIN selected_products s USING (product_id)
          WHERE p.product_locale = 'us'
          ORDER BY p.product_id
        ) TO '{sql_path(products_output)}' (FORMAT JSON, ARRAY false)
        """
    )
    connection.execute(
        f"""
        COPY (
          SELECT r.query_id, min(r.query) AS query,
                 json_group_object(r.product_id, r.esci_label) AS judgments,
                 min(r.split) AS split,
                 'Amazon ESCI public relevance labels' AS provenance
          FROM (
            SELECT r.*
            FROM ranked_judgments r
            JOIN selected_products p USING (product_id)
            ORDER BY r.query_id, r.product_id
          ) r
          GROUP BY r.query_id
          ORDER BY r.query_id
        ) TO '{sql_path(queries_output)}' (FORMAT JSON, ARRAY false)
        """
    )
    product_count = connection.execute(f"SELECT count(*) FROM read_json_auto('{sql_path(products_output)}', format='newline_delimited')").fetchone()[0]
    query_count = connection.execute(f"SELECT count(*) FROM read_json_auto('{sql_path(queries_output)}', format='newline_delimited')").fetchone()[0]
    judgment_count = connection.execute("SELECT count(*) FROM ranked_judgments r JOIN selected_products p USING (product_id)").fetchone()[0]
    connection.close()
    if product_count != args.product_limit or query_count != args.query_limit:
        raise ValueError(f"Output count mismatch: products={product_count}, queries={query_count}")

    manifest = {
        "dataset": "Amazon Shopping Queries Dataset (ESCI)",
        "license": "Apache-2.0",
        "locale": "us",
        "sample_seed": args.seed,
        "product_limit": args.product_limit,
        "query_limit": args.query_limit,
        "product_count": product_count,
        "query_count": query_count,
        "retained_judgment_count": judgment_count,
        "sources": {
            "products": {"file": products_source.name, "sha256": digest(products_source)},
            "examples": {"file": examples_source.name, "sha256": digest(examples_source)},
        },
        "outputs": {
            "products.jsonl": digest(products_output),
            "queries.jsonl": digest(queries_output),
        },
        "boundary": {
            "real": ["product title", "brand", "description", "bullet point", "color", "query", "product ID", "ESCI relevance label"],
            "simulated": ["price", "inventory", "category", "popularity", "placeholder hue", "users", "clicks", "orders"],
        },
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
