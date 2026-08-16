# Local runbook

## Two ways to run the stack

| Path | Command | What runs where | Use it for |
| --- | --- | --- | --- |
| Containers | `make up` | All eight Compose services in Docker | Demo and CI — one command brings up the whole system |
| Local processes | `make infra-up` + `make dev-*` | Only `postgres`, `redis`, `elasticsearch` in Docker; the five application services as host processes | Development — edit code without rebuilding images |

Both paths use the same `.env`. The local path additionally layers `dev.env` on top, which
rewrites the Docker network hostnames to `localhost` (`DATABASE_URL`, `REDIS_URL`,
`ELASTICSEARCH_URL`, `SEARCH_SERVICE_URL`, `COMMERCE_SERVICE_URL`, `AI_ADAPTER_URL`). `dev.env`
holds no credentials; secrets stay in `.env`.

## Start — container path

Run `make doctor`, `make bootstrap`, `make data`, `make up`, `make seed`, then
`make evaluate`. The first data download and container build are the long steps. Subsequent
runs reuse the download cache and named volumes.

## Start — local-process path

```bash
make infra-up      # postgres:5432 · redis:6379 · elasticsearch:9200, then prints dev-info
make dev-search    # :8080  Spring Boot, JDK 21 selected by the Makefile
make dev-ai        # :8000  uvicorn --reload
make dev-commerce  # :9000
make dev-store     # :3000  Next.js HMR
make dev-console   # :3001  Next.js HMR
```

Each `dev-*` target runs in the foreground, so give each one its own terminal. Start only the
services you are working on; anything else can keep running in its container
(`docker compose up -d <service>`). `make dev-info` reprints this list at any time.
`make infra-down` stops the three infrastructure containers and keeps their volumes.

## Health checks

```bash
curl -fsS http://localhost:3000/api/health                        # storefront
curl -fsS http://localhost:3001/api/health                        # operations console
curl -fsS http://localhost:8000/ai/health                         # AI adapter (reports provider)
curl -fsS http://localhost:8080/actuator/health/readiness         # search service
curl -fsS http://localhost:9000/health                            # commerce
curl -fsS http://localhost:9200/_cluster/health                   # elasticsearch
```

On the container path `docker compose ps` should show eight healthy containers, and `make logs`
follows only application logs. On the local-process path only three containers exist; the
application checks above answer only for the `dev-*` processes you actually started.

## Checking the AI path

AI is an optional enhancement. Search always answers, with or without it. Every
`/api/v1/search` response reports what happened in `ai_status`:

```bash
curl -s "http://localhost:8080/api/v1/search?q=noise%20cancelling%20headphones&size=1&use_ai=true"
```

| `ai_status` | Reading |
| --- | --- |
| `APPLIED` | Adapter answered and the query was rewritten (`ai_applied=true`) |
| `NO_CHANGE` | Adapter answered but returned an equivalent query — healthy, just no rewrite |
| `NOT_REQUESTED` | The request did not pass `use_ai=true`. Default for `/api/v1/search` |
| `DISABLED` | `AI_ENABLED=false`. The adapter is never contacted |
| `TIMEOUT` | Connect or read timeout — check the adapter, `AI_TIMEOUT_MS` and `AI_CONNECT_TIMEOUT_MS` |
| `TRANSPORT_ERROR` | Adapter unreachable, or it returned 4xx/5xx |
| `INVALID_RESPONSE` | Adapter replied but the body had no usable `rewritten_query` |

The last three are genuine failures; the request still returns HTTP 200 with BM25 results, so
`ai_status` — not the status code — is the signal. Each of those three also logs a `WARN` from
`lab.searchops.service.AiAdapterClient` with the status, request ID and underlying exception
(`DISABLED` and `NOT_REQUESTED` never contact the adapter and log nothing). `ai_provider` is
present only when a provider actually answered; the key is omitted otherwise.

`ai_applied=true` means the query text was really changed, not merely that the call succeeded.
A healthy adapter that matches no rewrite rule reports `ai_applied=false, ai_status=NO_CHANGE`.

## AI configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `AI_ENABLED` | `true` | Kill switch; `false` ⇒ `ai_status=DISABLED`, pure BM25 |
| `AI_ADAPTER_URL` | `http://ai-adapter:8000` | Adapter base URL; `dev.env` overrides to `http://localhost:8000` |
| `AI_TIMEOUT_MS` | `5000` | Read timeout waiting for the rewrite |
| `AI_CONNECT_TIMEOUT_MS` | `1000` | Connect timeout, budgeted separately so an unreachable adapter fails fast |
| `AI_PROVIDER` | `mock` | `mock` or `module.path:ClassName` |
| `AI_MODEL`, `AI_API_KEY`, `AI_API_BASE_URL`, `AI_TEMPERATURE`, `AI_MAX_TOKENS`, `AI_REQUEST_TIMEOUT_MS` | see `.env.example` | Reserved for a real provider; the mock ignores them. Keep `AI_REQUEST_TIMEOUT_MS` below `AI_TIMEOUT_MS` |

Non-positive timeout values fall back to the built-in defaults (read 5000 ms, connect 1000 ms),
with a 50 ms floor, so a mistyped `0` cannot disable the AI path by accident.

## Common recovery

- Missing catalog: run `make data && make seed`; seed is safe to repeat.
- Search returns index-not-found: check Elasticsearch health, then run `make seed`.
- AI unavailable: set `AI_ENABLED=false` and restart `search-service`; BM25 remains active and
  responses report `ai_status=DISABLED`. Stopping the adapter without flipping the switch works
  too — responses then report `TRANSPORT_ERROR` or `TIMEOUT` instead.
- Every AI search silently returns BM25 results: read `ai_status` before suspecting quality.
  `DISABLED` means the kill switch is on, `TIMEOUT` means the budget is too small for the
  provider, `INVALID_RESPONSE` means the adapter's body has no usable `rewritten_query`.
- Database schema error: inspect service logs. Migrations run automatically; do not edit the
  schema manually.
- Port collision: stop the conflicting local process or change the left-hand host port in
  `docker-compose.yml`. On the local-process path, the conflict is usually a container still
  holding 8080/8000 — stop that one service, not the whole stack.
- Policy publish rejected: approve that exact draft and use the returned token. Tokens are
  version-specific and are not logged.

## Evaluation

`make evaluate` sends the first deterministic `EVALUATION_QUERY_LIMIT` processed judgments
to the live Java service, persists per-query and aggregate metrics, and writes
`data/processed/evaluation-latest.json`. Larger values are more representative and slower.
The console shows the latest persisted run and low-NDCG slices.

`make evaluate-ai` runs the same evaluation with AI query rewriting enabled and writes
`data/processed/evaluation-ai-latest.json`, so the two runs never overwrite each other. The
request-level `use_ai` flag defaults to **false**; only `--use-ai` turns it on. That default is
what keeps new runs comparable with the archived baseline at the repository root,
`../../baselines/evaluation-v7-bm25-baseline-20260802.json` (strategy version 7, 200 queries, no
AI: NDCG@10 0.4326, Recall@10 0.5485, P@10 0.1115, MRR@10 0.4517). Never edit or delete files
under `baselines/`.

Two differences on the AI side worth knowing before you run it:

- It first sends one real search with `use_ai=true` and aborts, writing nothing, if `ai_status`
  comes back as anything other than `APPLIED` or `NO_CHANGE` — a fully degraded run cannot be
  saved under an AI label. A response carrying no `ai_status` at all does not abort the run:
  the guard only fires on a status it recognises as degraded, so an older search-service build
  that predates the field will still be evaluated. The probe reflects the state at start-up
  only, not the whole run, so an adapter that dies mid-run still degrades silently.
- `--persist` defaults to false for AI runs, so they do not appear in the console's evaluation
  history. `quality_metrics` is keyed by `(query_id, strategy_version)` and upserted, so
  persisting an AI run would overwrite that version's per-query BM25 rows. Pass `--persist`
  deliberately.

Direct script usage, when the make targets are too coarse:

```bash
./data/scripts/evaluate.sh --use-ai --limit 20 --output /tmp/ai-smoke.json
./data/scripts/evaluate.sh --limit 5 --no-persist --output /tmp/bm25-smoke.json
```

`evaluate.sh` sources `.env` and forwards any extra arguments to `evaluate.py`, so a later
`--limit` overrides `EVALUATION_QUERY_LIMIT`. `--search-url` defaults to
`http://localhost:8080`; use it to point the evaluation somewhere else.

## Shutdown and cleanup

`make down` preserves named volumes. `make clean-local` removes only resources owned by the
Compose project after checking the root marker, plus this repository's `data/raw`,
`data/processed` and `.runtime` content. It never touches files outside the project. Downloaded
and processed data can be recreated with `make data`. On the local-process path, stop the
`dev-*` processes yourself first — they are not Compose-managed.

## Backup / demo reset

For a complete demo reset, run `make clean-local`, then
`make data && make up && make seed && make evaluate`. Strategy and audit state will return to
the baseline because PostgreSQL's project volume is recreated.
