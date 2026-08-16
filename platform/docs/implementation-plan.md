# Commerce SearchOps Lab — implementation plan

Last updated: 2026-08-02

## Delivery stages

| Stage | Scope | Exit check | Status |
| --- | --- | --- | --- |
| 1 | Host, repository, runtime and resource inspection | Tool versions and constraints recorded | Complete |
| 2 | Architecture, ports, data flow and ADRs | Documents reviewed against the requested boundaries | Complete |
| 3 | Docker Compose and service skeletons | Every container reports healthy/ready | Complete |
| 4 | ESCI download, validation, deterministic sample and import | Re-running data and seed is idempotent | Complete |
| 5 | Browse, product, cart, checkout and order | Storefront happy-path integration test passes | Complete |
| 6 | BM25, filters, policy application and offline evaluation | Unit/integration tests and evaluation command pass | Complete |
| 7 | Operations console and governed policy lifecycle | Publish changes results and rollback restores them | Complete |
| 8 | AI mock, shared contracts, clients and tool gateway | Contract tests pass without an API key | Complete |
| 9 | Full test suite, Playwright, runbook and handoff | Acceptance checklist is evidenced | Complete |

## Environment findings

- Host: macOS 26.5.2 on Apple silicon, 12 logical CPUs, 32 GiB host memory.
- Docker Desktop: 28.3.2 / Compose 2.38.2, 12 CPUs and 8 GiB assigned to its VM.
- Node: host v25.7.0. Containers use Node 24 LTS because Medusa and its storefront support LTS releases through v24.
- Java: Microsoft OpenJDK 21.0.9 is installed; Corretto 8 is the host default. Maven and containers explicitly select Java 21.
- Python: 3.10.10 and uv 0.8.0 are installed. The service container uses Python 3.13.
- The workspace started with only `AGENTS.md` and no Git repository.

## Verification policy

At each stage we run the smallest useful unit/contract checks first, then container integration checks. The final gate runs `make test`, `make test-e2e`, and `make evaluate`. Evaluation output is written only by the evaluator; documentation never contains invented scores.

## Completion evidence

- `make doctor` passed with Docker 28.3.2, Compose 2.38.2, Java 21.0.9, Maven 3.9.12, Python 3.10.10 and the pinned container runtimes.
- `make data` was repeated from the local official-file cache. It reproduced 20,000 products, 10,000 queries, 22,724 retained judgments and identical output hashes.
- `make seed` was repeated. Commerce remained at 20,000 products, Elasticsearch remained at 20,000 documents, and 100 idempotently identified simulated search requests were recorded.
- `make test`, `make test-e2e`, and `make evaluate` passed on 2026-08-02. The measured evaluation artifact is `data/processed/evaluation-latest.json`; it is generated locally and intentionally ignored by Git.
- The policy integration test proved a published pin changed the live Elasticsearch ranking, rollback restored the prior top five, and create/submit/approve/publish/rollback events were present in the audit ledger.
- The AI adapter was stopped during a live check; BM25 continued to return results with `ai_applied=false`, then the adapter was restored and all eight services returned healthy.

## Scope decisions

- The Medusa runtime owns the commerce API surface. A small custom commerce module stores carts and demo orders in PostgreSQL so ESCI catalog ingestion stays fast and idempotent; prices and inventory remain deterministic simulations.
- The Java search service is the only component allowed to mutate Elasticsearch indices or search policy state.
- Policy publication is transactional in PostgreSQL. Search requests load the published version and compile it into the Elasticsearch query.
- The AI adapter is optional. Search uses BM25 when it is absent, slow, or disabled.
- Agent writes reuse governed policy endpoints and cannot call Elasticsearch directly.

## Risks and controls

- The upstream product parquet is about 1.03 GB. It is cached in `data/raw/`, verified, and ignored by Git. `PRODUCT_LIMIT` and `QUERY_LIMIT` bound the local subset.
- Docker has 8 GiB assigned. Elasticsearch uses a 1 GiB heap; other containers have conservative memory limits.
- Search policies may create surprising relevance changes. Preview is read-only; publish requires an approved version plus its approval token; every mutation requires an idempotency key and produces an append-only audit event.
