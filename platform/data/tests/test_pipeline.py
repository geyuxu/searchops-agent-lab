import json
import subprocess
import sys
from pathlib import Path

import duckdb


def make_sources(raw: Path) -> None:
    raw.mkdir()
    db = duckdb.connect()
    db.execute("""
        CREATE TABLE products AS SELECT * FROM (VALUES
          ('p1','Red running shoes','Light shoes','running red shoe','Acme','Red','us'),
          ('p2','Blue trail shoes','Trail sole','blue shoe','Acme','Blue','us'),
          ('p3','Coffee mug','Stoneware','ceramic mug','Home','White','us'),
          ('p4','Desk lamp','LED lamp','light','Bright','Black','us')
        ) t(product_id, product_title, product_description, product_bullet_point, product_brand, product_color, product_locale)
    """)
    db.execute("""
        CREATE TABLE examples AS SELECT * FROM (VALUES
          (1,'running shoes',10,'p1','us','E',1,1,'train'),
          (2,'running shoes',10,'p2','us','S',1,1,'train'),
          (3,'coffee mug',20,'p3','us','E',1,1,'test'),
          (4,'coffee mug',20,'p4','us','I',1,1,'test')
        ) t(example_id, query, query_id, product_id, product_locale, esci_label, small_version, large_version, split)
    """)
    db.execute(f"COPY products TO '{raw / 'shopping_queries_dataset_products.parquet'}' (FORMAT PARQUET)")
    db.execute(f"COPY examples TO '{raw / 'shopping_queries_dataset_examples.parquet'}' (FORMAT PARQUET)")
    db.close()


def test_pipeline_is_deterministic_and_preserves_labels(tmp_path: Path):
    raw = tmp_path / "raw"
    output = tmp_path / "processed"
    make_sources(raw)
    script = Path(__file__).parents[1] / "scripts" / "process.py"
    command = [sys.executable, str(script), "--raw-dir", str(raw), "--output-dir", str(output),
               "--product-limit", "3", "--query-limit", "2", "--seed", "test-seed"]
    subprocess.run(command, check=True, capture_output=True, text=True)
    first_products = (output / "products.jsonl").read_bytes()
    first_queries = (output / "queries.jsonl").read_bytes()
    subprocess.run(command, check=True, capture_output=True, text=True)
    assert (output / "products.jsonl").read_bytes() == first_products
    assert (output / "queries.jsonl").read_bytes() == first_queries

    queries = [json.loads(line) for line in first_queries.decode().splitlines()]
    assert len(queries) == 2
    assert all(query["judgments"] for query in queries)
    assert {label for query in queries for label in query["judgments"].values()} <= {"E", "S", "C", "I"}
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["product_count"] == 3
    assert manifest["query_count"] == 2
    assert "price" in manifest["boundary"]["simulated"]

