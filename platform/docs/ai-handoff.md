# AI implementation handoff

The system is complete without a model key. The default provider is deterministic, returns
`provider=mock`, and is covered by contract tests. AI calls are an optional enhancement to the
Java BM25 path, never a startup dependency.

## Where to work

- `services/ai-adapter/app/provider.py` — provider interface, `load_provider`, current mock.
- `services/ai-adapter/app/models.py` — validated transport models. Keep these stable.
- `services/ai-adapter/app/main.py` — HTTP boundary, request IDs, logs and metrics.
- `packages/api-contracts/openapi/ai-adapter.openapi.json` — canonical OpenAPI contract.
- `packages/api-contracts/json-schema/ai-contracts.schema.json` — standalone schemas.
- `packages/api-contracts/typescript` and `packages/api-contracts/java` — consumers.
- `services/search-service/.../service/AiAdapterClient.java` — timeout, admission and fallback.
- `services/search-service/.../domain/AiRewriteStatus.java` — the seven `ai_status` values.
- `services/search-service/.../config/HttpConfig.java` — connect and read timeouts.

Add RAG, LangGraph, prompt/evaluation code in new modules under
`services/ai-adapter/app/providers/` and `services/ai-adapter/app/workflows/`. Keep provider
secrets and model-specific types behind the `Provider` interface. Do not add model concerns to
the storefront, operations console or Java domain contracts.

## What the Java client accepts from a provider

`AiAdapterClient.rewrite` applies exactly one admission condition to the adapter response:
**the response must carry a non-blank `rewritten_query`**. Nothing else gates the result.

- The `provider` field is recorded and forwarded, never validated. Any name is accepted
  (`mock`, `langchain`, `openai`, your own). It is surfaced on the search response as
  `ai_provider`.
- If `provider` is missing or blank the client logs a warning and reports the provider as
  `unknown`. The rewrite is still used — a missing provider name does not cause a fallback.
- If `rewritten_query` is blank or absent, the client raises `InvalidAiResponseException` and
  falls back to BM25 with `ai_status=INVALID_RESPONSE`.

This is what makes the handoff promise below true. Earlier revisions of the client required
`provider == "mock"`, so every real provider was rejected and silently downgraded to BM25.
That check is gone.

## Replacing the mock

1. Implement `app.provider.Provider`: `rewrite`, `rerank`, `suggest`, and a stable
   `name`. Return the existing Pydantic model shapes.
2. Package any model SDK in `requirements.txt` with an exact version. Read credentials only
   from environment variables; add blank names, never values, to `.env.example`.
3. Set `AI_PROVIDER=your.module:YourProvider`. `load_provider` accepts `mock` or a
   `module.path:ClassName` string and rejects anything that is not a `Provider` subclass.
4. Fill in the reserved real-provider variables in `.env` (see the table below). They are
   already declared in `.env.example` and wired into the `ai-adapter` service in
   `docker-compose.yml`, so no compose edit is needed.
5. Run `services/ai-adapter/.venv/bin/pytest` and the shared contract test.
6. Exercise timeouts, malformed upstream responses and provider failure. The Java client must
   still fall back to BM25, and the reason must show up in `ai_status` — see the table below.
7. Run `make test`, then `make evaluate` (BM25 baseline) and `make evaluate-ai` (AI candidate)
   and compare the two output files. Do not publish strategy changes automatically.

**No other service needs an interface change to swap in a real provider.** The storefront, the
operations console and the Java domain contracts do not read the provider name and do not
branch on it; the search service only requires a usable `rewritten_query`. `AI_ENABLED=false`
remains the kill switch, and the search path stays on BM25 whenever the adapter is missing,
slow or wrong.

## Observability: `ai_status` and `ai_applied`

Every `/api/v1/search` response carries `ai_status` (never null) and `ai_applied`. `ai_provider`
is present only when a provider actually answered; the service serialises with
`spring.jackson.default-property-inclusion=non_null`, so the key is omitted otherwise.

| `ai_status` | Adapter called? | Fallback to BM25 | Meaning |
| --- | --- | --- | --- |
| `APPLIED` | yes | no | Adapter answered and the rewritten query differs from the original |
| `NO_CHANGE` | yes | no | Adapter answered but returned an equivalent query (no rule matched) |
| `NOT_REQUESTED` | no | n/a | The request did not ask for AI (`use_ai=false`). Not a failure |
| `DISABLED` | no | n/a | Kill switch `AI_ENABLED=false` (`lab.search.ai-enabled`). Not a failure |
| `TIMEOUT` | attempted | yes | Connect or read timeout |
| `TRANSPORT_ERROR` | attempted | yes | Other network failure, or the adapter returned 4xx/5xx |
| `INVALID_RESPONSE` | attempted | yes | Body missing, not valid JSON, or no usable `rewritten_query` |

`AiRewriteStatus.fallback()` is true for everything except `APPLIED` and `NO_CHANGE`;
`failure()` narrows that to the three genuine failures, so `DISABLED` and `NOT_REQUESTED` are
not counted as incidents.

`ai_applied` means **the query was actually rewritten** — the rewritten text, after trimming,
whitespace folding and lower-casing, differs from the original. It no longer means "the AI call
did not throw". A provider that returns the query unchanged now yields
`ai_applied=false, ai_status=NO_CHANGE`, which is the honest reading: AI ran and changed
nothing. Anything that used `ai_applied` as a health signal should read `ai_status` instead.

The three failure states each emit a `WARN` from `lab.searchops.service.AiAdapterClient`
carrying the status, the request ID and the underlying exception. `DISABLED` and
`NOT_REQUESTED` return without contacting the adapter and log nothing.

## Timeouts

Two separate budgets, both read from the environment and both overridable:

| Variable | Default | Applies to |
| --- | ---: | --- |
| `AI_CONNECT_TIMEOUT_MS` | 1000 | Establishing the TCP connection to the adapter |
| `AI_TIMEOUT_MS` | 5000 | Waiting for the adapter's response body |

`HttpConfig` builds the AI `RestClient` with the connect timeout on the underlying
`java.net.http.HttpClient` and the read timeout on `JdkClientHttpRequestFactory`. Non-positive
values fall back to the built-in defaults (read 5000 ms, connect 1000 ms) and a 50 ms floor is
enforced, so a misconfigured `0` cannot make the AI path fail by construction. Both a connect
timeout and a read timeout classify as `ai_status=TIMEOUT`.

The read timeout was previously 400 ms — enough for the deterministic mock, but below any real
model's first-byte latency. Under that budget a real provider times out on every call, falls
back to BM25, and the request still returns HTTP 200, so "the provider never once succeeded"
reads exactly like "AI gives no benefit". Budget for your provider's p99 first-byte latency, and
keep the adapter's own model-call timeout (`AI_REQUEST_TIMEOUT_MS`, default 4000) below
`AI_TIMEOUT_MS` so the adapter is not still working after Java has given up.

## Environment variables

Declared in `.env.example`; real values belong only in the git-ignored `.env`.

| Variable | Default | Read by | Purpose |
| --- | --- | --- | --- |
| `AI_ENABLED` | `true` | search-service | Kill switch. `false` ⇒ `ai_status=DISABLED` |
| `AI_ADAPTER_URL` | `http://ai-adapter:8000` | search-service | Adapter base URL (`dev.env` overrides it to `http://localhost:8000` for the local-process path) |
| `AI_TIMEOUT_MS` | `5000` | search-service | Read timeout |
| `AI_CONNECT_TIMEOUT_MS` | `1000` | search-service | Connect timeout |
| `AI_PROVIDER` | `mock` | ai-adapter | `mock` or `module.path:ClassName` |
| `AI_MODEL` | empty | ai-adapter | Reserved for a real provider; the mock ignores it |
| `AI_API_KEY` | empty | ai-adapter | Reserved. Empty means unconfigured — a real provider should fail loudly at startup rather than degrade silently |
| `AI_API_BASE_URL` | empty | ai-adapter | Reserved: gateway/proxy/compatible endpoint |
| `AI_TEMPERATURE` | `0` | ai-adapter | Reserved. Keep at 0 for reproducible evaluation |
| `AI_MAX_TOKENS` | `512` | ai-adapter | Reserved: output cap per rewrite |
| `AI_REQUEST_TIMEOUT_MS` | `4000` | ai-adapter | Reserved: provider-side model call timeout; keep below `AI_TIMEOUT_MS` |

The reserved variables are already listed in the `ai-adapter` service's `environment:` block in
`docker-compose.yml`; that service has no `env_file`, so anything not listed there never reaches
the container.

## Measuring an AI change

`EvaluationRequest.use_ai` defaults to **false** and must stay that way: the archived baseline
at the repository root, `../../baselines/evaluation-v7-bm25-baseline-20260802.json` (strategy
version 7, 200 queries, no AI — NDCG@10 0.4326, Recall@10 0.5485, P@10 0.1115, MRR@10 0.4517),
is only comparable to later runs if the default is unchanged. `EvaluationService` now passes the
request's flag through to `SearchOptions.useAi` instead of hard-coding `false`, so that flag is
the only thing deciding whether the AI path is exercised.

Run the two sides separately — they write different files and neither overwrites the other:

```bash
make evaluate      # BM25 side  -> data/processed/evaluation-latest.json
make evaluate-ai   # AI side    -> data/processed/evaluation-ai-latest.json
```

Both files contain a per-query `queries` array keyed by `query_id`, so the two runs can be
paired for a per-query comparison. Every run — AI or baseline — records an `evaluation_client`
block (`use_ai`, `ai_provider`, `ai_status`, `query_limit`, `persist`, `client_timeout_seconds`,
`search_url`) alongside the server's own `use_ai` echo. On a baseline run the AI fields are
absent or null because no probe is sent, which is itself how you tell the two runs apart.

Three behaviours to know before you read the numbers:

- **A pre-flight probe guards the run.** Before sending the query set, `evaluate.py` issues one
  real `GET /api/v1/search?...&use_ai=true` and reads `ai_status`. Any value other than
  `APPLIED` or `NO_CHANGE` aborts without writing a file, so a wholly degraded run cannot be
  saved with an AI label. A response carrying no `ai_status` at all — an older build — is not
  treated as a failure and the run proceeds. The probe reports the state at start-up only: it is
  not an aggregate over the run, and it cannot detect an adapter that dies midway.
- **`--use-ai` defaults `--persist` to false.** `quality_metrics` is keyed by
  `(query_id, strategy_version)` and written with `ON CONFLICT DO UPDATE`, so persisting an AI
  run would overwrite that version's per-query BM25 rows, which is what the console's low-NDCG
  view reads. Pass `--persist` explicitly if you want the AI run in the database, and bump the
  strategy version first if you still need the BM25 rows.
- **The mock produces no delta on the default slice, by construction.** `MockProvider` expands
  only `tv`, `laptop`, `headphones`, `sneakers`, `cellphone` and an `under|below|less than $N`
  price phrase. In the generated `data/processed/queries.jsonl`, the first query matching any of
  those rules is on line 268 — past the default 200-query slice. So `make evaluate-ai` against
  the default slice reports `ai_provider=mock`, `ai_status=NO_CHANGE` and aggregates identical
  to the archived baseline, which is what the last local run wrote to
  `data/processed/evaluation-ai-latest.json`. Do not read that zero delta as "AI has no
  benefit"; it means the mock had nothing to rewrite. To measure anything, use a real provider
  or point `--queries` at a slice that hits the rules.

## Contract semantics

- Query rewrite may normalize text and extract filters, but must echo the original query and
  request ID context.
- Rerank accepts only bounded candidates supplied by search and returns product IDs from that
  candidate set.
- Strategy suggestion produces evidence-linked proposed changes. It never writes a strategy.
- Provider latency excludes client network time; Java separately observes end-to-end timeout.
- Explanations are operator diagnostics, not trusted facts.

## Recommended first AI increment

Start with an offline-measured query rewriter for zero-result and low-recall ESCI queries. Keep
the BM25 candidate generator fixed, write a dataset of rewrite decisions, and gate promotion on
NDCG@10/Recall@10 plus a no-regression slice. Next add a candidate-only reranker. Add the
SearchOps Agent last, using the governed tools and approval workflow documented below.

## Known gaps

- `EvaluationResult` does not aggregate per-query `ai_status`, so a run cannot report how many
  of its queries were actually rewritten or how many degraded mid-run. Adding that is a Java
  change in `EvaluationService`.
- `EvaluationService.run` is a serial loop, so wall-clock time for an AI run grows linearly with
  query count, worst case `n × (AI_CONNECT_TIMEOUT_MS + AI_TIMEOUT_MS)`. The evaluation client
  sizes its timeout for that worst case rather than parallelising.

## Agent safety boundary

The future agent calls `/api/v1/tools/*`. It cannot access Elasticsearch mutation endpoints.
Preview and single-query evaluation are read-only. Every write supplies actor, request ID and
`Idempotency-Key`; publish/rollback additionally require an approval token. Audit events are
append-only in PostgreSQL. An agent may propose and submit, but production automation should
leave approval and publish to a human role.
