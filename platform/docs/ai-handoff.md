# AI implementation handoff

The system is complete without a model key. The current provider is deterministic, returns
`provider=mock`, and is covered by contract tests. AI calls are an optional enhancement to the
Java BM25 path, never a startup dependency.

## Where to work

- `services/ai-adapter/app/provider.py` — provider interface and current mock.
- `services/ai-adapter/app/models.py` — validated transport models. Keep these stable.
- `services/ai-adapter/app/main.py` — HTTP boundary, request IDs, logs and metrics.
- `packages/api-contracts/openapi/ai-adapter.openapi.json` — canonical OpenAPI contract.
- `packages/api-contracts/json-schema/ai-contracts.schema.json` — standalone schemas.
- `packages/api-contracts/typescript` and `packages/api-contracts/java` — consumers.
- `services/search-service/.../AiAdapterClient.java` — timeout/fallback behavior.

Add RAG, LangGraph, prompt/evaluation code in new modules under
`services/ai-adapter/app/providers/` and `services/ai-adapter/app/workflows/`. Keep provider
secrets and model-specific types behind the `Provider` interface. Do not add model concerns to
the storefront, operations console or Java domain contracts.

## Replacing the mock

1. Implement `app.provider.Provider`: `rewrite`, `rerank`, `suggest`, and a stable
   `name`. Return the existing Pydantic model shapes.
2. Package any model SDK in `requirements.txt` with an exact version. Read credentials only
   from environment variables; add blank names, never values, to `.env.example`.
3. Set `AI_PROVIDER=your.module:YourProvider`.
4. Run `services/ai-adapter/.venv/bin/pytest` and the shared contract test.
5. Exercise timeouts, malformed upstream responses and provider failure. The Java client must
   still fall back to BM25 within `AI_TIMEOUT_MS`.
6. Run `make test`, `make evaluate`, and compare a persisted baseline run against an
   AI-enabled candidate. Do not publish strategy changes automatically.

No other service needs an interface change. `AI_ENABLED=false` is the kill switch.

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

## Agent safety boundary

The future agent calls `/api/v1/tools/*`. It cannot access Elasticsearch mutation endpoints.
Preview and single-query evaluation are read-only. Every write supplies actor, request ID and
`Idempotency-Key`; publish/rollback additionally require an approval token. Audit events are
append-only in PostgreSQL. An agent may propose and submit, but production automation should
leave approval and publish to a human role.

