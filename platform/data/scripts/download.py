#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

FILES = {
    "products": {
        "name": "shopping_queries_dataset_products.parquet",
        "url": "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset/shopping_queries_dataset_products.parquet",
        "min_bytes": 900_000_000,
        "checksum_env": "ESCI_PRODUCTS_SHA256",
    },
    "examples": {
        "name": "shopping_queries_dataset_examples.parquet",
        "url": "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset/shopping_queries_dataset_examples.parquet",
        "min_bytes": 40_000_000,
        "checksum_env": "ESCI_EXAMPLES_SHA256",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(path: Path, expected: str, minimum: int) -> str:
    if path.stat().st_size < minimum:
        raise ValueError(f"{path.name} is unexpectedly small ({path.stat().st_size} bytes)")
    with path.open("rb") as source:
        if source.read(4) != b"PAR1":
            raise ValueError(f"{path.name} has no parquet header")
        source.seek(-4, 2)
        if source.read(4) != b"PAR1":
            raise ValueError(f"{path.name} has no parquet footer")
    actual = sha256(path)
    if expected and actual.lower() != expected.lower():
        raise ValueError(f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}")
    return actual


def download(url: str, destination: Path) -> None:
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Commerce-SearchOps-Lab/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        total = int(response.headers.get("content-length", "0"))
        copied = 0
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            copied += len(chunk)
            if total and copied % (50 * 1024 * 1024) < len(chunk):
                print(f"{destination.name}: {copied / total:.0%}", file=sys.stderr)
    partial.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, default=Path(__file__).resolve().parents[1] / "raw")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for key, metadata in FILES.items():
        destination = args.raw_dir / metadata["name"]
        if args.force or not destination.exists():
            print(f"Downloading official ESCI {key} parquet…", file=sys.stderr)
            download(metadata["url"], destination)
        results[key] = {
            "path": str(destination),
            "url": metadata["url"],
            "bytes": destination.stat().st_size,
            "sha256": validate(
                destination,
                os.getenv(metadata["checksum_env"], ""),
                int(metadata["min_bytes"]),
            ),
        }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

