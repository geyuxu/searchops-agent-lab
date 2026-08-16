# Commerce SearchOps Lab

A local, end-to-end commerce search laboratory built around the public Amazon Shopping Queries
(ESCI) dataset. It combines a Medusa commerce runtime, two Next.js applications, a Java 21
search and SearchOps service, Elasticsearch, PostgreSQL, Redis, and a keyless FastAPI AI mock.

> **Data boundary:** product text, queries, product IDs and relevance labels come from the
> public ESCI dataset. Prices, inventory, categories, popularity, users, traffic, carts and
> orders are deterministic simulations. They are not Amazon transaction data. Product art is
> generated locally and does not hotlink Amazon images.

## Quick start from a clean checkout

Requirements: Docker Desktop with at least 8 GB assigned, Python 3.10+, Node 22–24 for local
builds, Maven 3.9+, and Java 21. On macOS, the scripts select an installed Java 21 even when the
system default is older.

```bash
make doctor
make bootstrap
make data
make up
make seed
make evaluate
```

`make data` downloads the official ESCI parquet files (roughly 1.1 GB), validates them, and
creates a deterministic US-locale subset. Defaults are 20,000 products and 10,000 queries.
Override them before processing, for example:

```bash
PRODUCT_LIMIT=5000 QUERY_LIMIT=2500 make data
EVALUATION_QUERY_LIMIT=100 make evaluate
```

Open the storefront at [http://localhost:3000](http://localhost:3000) and the SearchOps console
at [http://localhost:3001](http://localhost:3001). The first `make seed` is idempotent: it
upserts the commerce catalog and atomically rebuilds the versioned Elasticsearch index.

## Services

| Service | Address | Purpose |
| --- | --- | --- |
| Storefront | http://localhost:3000 | browse, search, cart, simulated checkout and orders |
| SearchOps console | http://localhost:3001 | metrics, preview, policy lifecycle and audit |
| Search API | http://localhost:8080 | BM25, evaluation, policy and agent tools |
| Search health | http://localhost:8080/actuator/health/readiness | readiness |
| Commerce / Medusa | http://localhost:9000 | carts and simulated orders |
| AI mock / docs | http://localhost:8000/docs | deterministic AI contracts |
| Elasticsearch | http://localhost:9200 | local search engine |

## Everyday commands

```text
make doctor       verify tools, Docker and resources
make bootstrap    install all pinned dependencies
make data         download, sample and validate ESCI
make up           build and start the complete stack
make down         stop the stack but preserve volumes
make seed         idempotently load PostgreSQL and Elasticsearch
make test         unit, contract and service integration tests
make test-e2e     Playwright storefront journey + SearchOps console smoke test
make evaluate     real P@10, R@10, MRR@10, NDCG@10 and zero-result rate
make logs         follow application logs
make clean-local  remove only this project's regenerable containers/volumes/runtime output
```

## SearchOps workflow

Policies support synonyms, exact rewrite rules, pinned and blocked products, brand boosts,
field weights and minimum score. Operators can preview without writing, then create a draft,
submit it, approve it, publish with the returned approval token, and roll back. Every write
requires an `Idempotency-Key`; publish and rollback reject missing or invalid approval state.
The Java service is the only code allowed to build Elasticsearch queries or indices.

The AI adapter is optional. Set `AI_ENABLED=false`, or stop the adapter after startup, and
search continues with BM25. Future providers implement the same contract, so storefront,
console and search clients do not change.

## Test and evidence policy

Offline numbers are never hard-coded in this repository. `make evaluate` evaluates the
processed ESCI judgments against the running index and writes the actual result to
`data/processed/evaluation-latest.json`. Simulated traffic is visibly labelled in both UIs.

See [architecture](docs/architecture.md), [data provenance](docs/data-provenance.md),
[runbook](docs/runbook.md), [AI handoff](docs/ai-handoff.md), and the
[future MCP design](docs/mcp-server-design.md).

## Repository map

- `apps/storefront` — shopper experience.
- `apps/operations-console` — SearchOps control room.
- `services/commerce` — Medusa runtime and PostgreSQL cart/order APIs.
- `services/search-service` — search, evaluation, strategies, audit and Agent Tool Gateway.
- `services/ai-adapter` — deterministic provider and replaceable AI boundary.
- `packages/api-contracts` — OpenAPI, JSON Schema and Java/TypeScript clients.
- `data` — repeatable ESCI pipeline; raw and processed files are ignored.
