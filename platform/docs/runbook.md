# Local runbook

## Start

Run `make doctor`, `make bootstrap`, `make data`, `make up`, `make seed`, then
`make evaluate`. The first data download and container build are the long steps. Subsequent
runs reuse the download cache and named volumes.

## Health checks

```bash
curl -fsS http://localhost:3000/api/health
curl -fsS http://localhost:3001/api/health
curl -fsS http://localhost:8000/ai/health
curl -fsS http://localhost:8080/actuator/health/readiness
curl -fsS http://localhost:9000/health
curl -fsS http://localhost:9200/_cluster/health
```

`docker compose ps` should show eight healthy containers. `make logs` follows only
application logs.

## Common recovery

- Missing catalog: run `make data && make seed`; seed is safe to repeat.
- Search returns index-not-found: check Elasticsearch health, then run `make seed`.
- AI unavailable: set `AI_ENABLED=false` and restart `search-service`; BM25 remains active.
- Database schema error: inspect service logs. Migrations run automatically; do not edit the
  schema manually.
- Port collision: stop the conflicting local process or change the left-hand host port in
  `docker-compose.yml`.
- Policy publish rejected: approve that exact draft and use the returned token. Tokens are
  version-specific and are not logged.

## Evaluation

`make evaluate` sends the first deterministic `EVALUATION_QUERY_LIMIT` processed judgments
to the live Java service, persists per-query and aggregate metrics, and writes
`data/processed/evaluation-latest.json`. Larger values are more representative and slower.
The console shows the latest persisted run and low-NDCG slices.

## Shutdown and cleanup

`make down` preserves named volumes. `make clean-local` removes only resources owned by the
Compose project after checking the root marker, plus this repository's `data/raw`,
`data/processed` and `.runtime` content. It never touches files outside the project. Downloaded
and processed data can be recreated with `make data`.

## Backup / demo reset

For a complete demo reset, run `make clean-local`, then
`make data && make up && make seed && make evaluate`. Strategy and audit state will return to
the baseline because PostgreSQL's project volume is recreated.
