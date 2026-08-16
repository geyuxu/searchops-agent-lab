# Data provenance and simulation boundary

## Public source

The baseline uses Amazon Science's **Amazon Shopping Queries Dataset (ESCI)**, distributed
under Apache-2.0 at <https://github.com/amazon-science/esci-data>. The pipeline downloads the
official product and example parquet artifacts referenced by that repository.

Retained without invention:

- product ID, locale, title, brand, description, bullet point and color;
- search query and query ID;
- ESCI label: Exact, Substitute, Complement or Irrelevant;
- upstream train/test split.

The default sample keeps US-locale rows marked `small_version=1`. Query IDs are ordered by a
seeded SHA-256 expression; each selected query retains at least its best-labelled available
product, then the product pool is filled deterministically up to `PRODUCT_LIMIT`. All labels
whose products remain in the pool are retained. The manifest records counts and SHA-256 hashes.

## Deterministic simulation

ESCI is a relevance dataset, not a transaction system. The following values are derived from a
stable hash of `product_id + DATA_SAMPLE_SEED`: USD price, inventory, display category,
popularity and placeholder hue. Users, search traffic, clicks, carts and orders are also demo
data. The same input and seed produce the same values.

These fields must always be presented as **simulated**, never as Amazon sales, behavioral,
inventory or price data. Both applications show this boundary in their permanent UI.

## Local-only artwork

No Amazon image URL is collected. Product artwork is a local CSS composition parameterized by
the simulated hue. It is intentionally generic and carries no product photo claim.

## Reproducibility and validation

`make data` performs four checks:

1. atomic download to `.part` followed by a parquet magic-byte and minimum-size check;
2. optional exact source SHA-256 validation with `ESCI_PRODUCTS_SHA256` and
   `ESCI_EXAMPLES_SHA256`;
3. deterministic processing with explicit product/query limits and seed;
4. schema, count, uniqueness, label-domain and boundary validation.

`data/processed/manifest.json` identifies source and output hashes. Re-running with unchanged
source files, parameters and DuckDB version is byte-for-byte deterministic. Raw and processed
artifacts are excluded from version control.

Amazon Reviews'23 is intentionally outside the MVP and is not required by any command.

