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

## Two run paths

| Path | Command | What runs where | Positioning |
| --- | --- | --- | --- |
| Containers | `make up` | All eight Compose services in Docker | **Demo and CI.** One command brings the whole system up; this is the acceptance path |
| Local processes | `make infra-up` + `make dev-*` | Only `postgres`, `redis`, `elasticsearch` in Docker; the five application services as host processes | **Development iteration.** No image rebuild per edit; hot reload for the adapter and both front ends, attachable debuggers |

Only the three stateful, version-sensitive components are worth containerising for day-to-day
work. The other five are application code, where a container round-trip costs a Maven image
rebuild or the loss of Next.js hot reload.

Both paths read the same `.env`. The `dev-*` targets layer `dev.env` on top of it, which
rewrites Docker network hostnames to `localhost`. `dev.env` contains no credentials and is safe
to commit; secrets stay in `.env`.

```bash
make infra-up      # three infra containers, waits for health, then prints dev-info
make dev-search    # :8080  search service (JDK 21 selected automatically)
make dev-ai        # :8000  AI adapter, uvicorn --reload
make dev-commerce  # :9000
make dev-store     # :3000  Next.js HMR
make dev-console   # :3001  Next.js HMR
```

Each `dev-*` target runs in the foreground — one terminal each. Start only what you are
changing; leave the rest running in containers.

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
make up           build and start the complete stack (demo / CI path)
make down         stop the stack but preserve volumes
make infra-up     start only postgres, redis and elasticsearch (development path)
make infra-down   stop those infrastructure containers, keep volumes
make dev-info     print how to run each application as a local process
make dev-search   run the search service locally on :8080
make dev-ai       run the AI adapter locally on :8000 with hot reload
make dev-commerce run the commerce service locally on :9000
make dev-store    run the storefront locally on :3000 with hot reload
make dev-console  run the operations console locally on :3001 with hot reload
make seed         idempotently load PostgreSQL and Elasticsearch
make test         unit, contract and service integration tests
make test-e2e     Playwright storefront journey + SearchOps console smoke test
make evaluate     real P@10, R@10, MRR@10, NDCG@10 and zero-result rate (BM25, no AI)
make evaluate-ai  the same evaluation with AI query rewriting enabled
make logs         follow application logs
make clean-local  remove only this project's regenerable containers/volumes/runtime output
```

## SearchOps workflow

Policies support synonyms, exact rewrite rules, pinned and blocked products, brand boosts,
field weights and minimum score. Operators can preview without writing, then create a draft,
submit it, approve it, publish with the returned approval token, and roll back. Every write
requires an `Idempotency-Key`; publish and rollback reject missing or invalid approval state.
The Java service is the only code allowed to build Elasticsearch queries or indices.

## The AI boundary

The AI adapter is optional. Set `AI_ENABLED=false`, or stop the adapter after startup, and
search continues with BM25. Swapping in a real provider means implementing
`app.provider.Provider` and setting `AI_PROVIDER=module.path:ClassName` — the storefront,
console and Java search clients do not change, because the search service's only requirement of
an adapter response is a usable `rewritten_query`. The provider name is recorded and reported,
never validated.

Every `/api/v1/search` response reports the outcome in `ai_status`, which is never null:
`APPLIED`, `NO_CHANGE`, `NOT_REQUESTED`, `DISABLED`, `TIMEOUT`, `TRANSPORT_ERROR`,
`INVALID_RESPONSE`. The first two mean the adapter answered; the last three are fallbacks to
BM25 that still return HTTP 200, so `ai_status` — not the status code — is what tells you AI
degraded. `ai_applied` means the query text was actually rewritten, so a healthy adapter that
matches no rule reports `ai_applied=false, ai_status=NO_CHANGE`. `ai_provider` appears only when
a provider actually answered.

AI-related settings, all declared in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_ENABLED` | `true` | Kill switch; `false` ⇒ `ai_status=DISABLED` |
| `AI_ADAPTER_URL` | `http://ai-adapter:8000` | Adapter base URL (`dev.env` points it at localhost) |
| `AI_TIMEOUT_MS` | `5000` | Read timeout for the rewrite call |
| `AI_CONNECT_TIMEOUT_MS` | `1000` | Connect timeout, budgeted separately for fast failure |
| `AI_PROVIDER` | `mock` | `mock` or `module.path:ClassName` |
| `AI_MODEL` / `AI_API_KEY` / `AI_API_BASE_URL` / `AI_TEMPERATURE` / `AI_MAX_TOKENS` / `AI_REQUEST_TIMEOUT_MS` | see `.env.example` | Reserved for a real provider; the mock ignores them |

See [AI handoff](docs/ai-handoff.md) for the full replacement procedure.

## Test and evidence policy

Offline numbers are never hard-coded in this repository. `make evaluate` evaluates the
processed ESCI judgments against the running index and writes the actual result to
`data/processed/evaluation-latest.json`. `make evaluate-ai` writes the AI-enabled candidate to
`data/processed/evaluation-ai-latest.json`, so the two sides never overwrite each other and can
be paired per `query_id`. The evaluation request's `use_ai` flag defaults to false, which is
what keeps new runs comparable with the archived baseline at the repository root,
`../baselines/evaluation-v7-bm25-baseline-20260802.json` (strategy version 7, 200 queries, no
AI: NDCG@10 0.4326, Recall@10 0.5485, P@10 0.1115, MRR@10 0.4517). Files under `baselines/` are
read-only archives. Simulated traffic is visibly labelled in both UIs.

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
