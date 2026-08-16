# ESCI data workspace

`make data` downloads the two official Amazon Shopping Queries Dataset parquet files into ignored `data/raw/`, validates their parquet framing and optional SHA-256 pins, then creates a deterministic US-English subset in ignored `data/processed/`.

Defaults are 20,000 products and 10,000 queries. Change `PRODUCT_LIMIT`, `QUERY_LIMIT`, or `DATA_SAMPLE_SEED` in `.env` or on the command line. The sample guarantees at least one highest-available relevance judgment per selected query, allocates the remaining product budget deterministically, and preserves every ESCI label whose product remains in the local index.

Generated files:

- `products.jsonl`: official ESCI text plus deterministic simulated commerce attributes.
- `queries.jsonl`: real query IDs/text and retained E/S/C/I judgments.
- `manifest.json`: source URLs, hashes, counts, limits and output hashes.
- `evaluation-latest.json`: created only by a real evaluation run.

Nothing under `raw/` or `processed/` is committed. No Amazon images, prices, inventory, users, clicks or orders are used or claimed.

