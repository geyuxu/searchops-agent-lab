# Stable API contracts

This package is the handoff boundary between the baseline commerce/search system and future AI work.

- `openapi/ai-adapter.openapi.json` is the HTTP contract implemented by `services/ai-adapter`.
- `json-schema/ai-contracts.schema.json` contains standalone request/response schemas.
- `typescript/` and `java/` contain timeout-aware clients.
- `examples/` contains keyless mock requests and deterministic response examples.
- `openapi/searchops-tools.openapi.json` documents the safe Agent Tool Gateway. It has no Elasticsearch mutation operation.

Breaking changes require a new API major version. Adding optional response fields is backward compatible.

