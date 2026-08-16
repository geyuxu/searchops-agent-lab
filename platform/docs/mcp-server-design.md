# Future MCP server design

MVP exposes an ordinary REST Agent Tool Gateway. An MCP layer should be a thin adapter, not a
second policy implementation.

| MCP tool | REST operation | Safety class |
| --- | --- | --- |
| `get_query_metrics` | `GET /tools/query-metrics` | read |
| `get_zero_result_queries` | `GET /tools/zero-result-queries` | read |
| `get_low_quality_queries` | `GET /tools/low-quality-queries` | read |
| `get_current_strategy` | `GET /tools/strategies/current` | read |
| `get_strategy_history` | `GET /tools/strategies/history` | read |
| `preview_strategy` | `POST /tools/strategies/preview` | dry-run |
| `evaluate_query` | `POST /tools/evaluations/query` | read/compute |
| `create_strategy_draft` | `POST /tools/strategies/drafts` | governed write |
| `submit_strategy` | `POST /tools/strategies/{id}/submit` | governed write |
| `approve_strategy` | `POST /tools/strategies/{id}/approve` | privileged write |
| `publish_strategy` | `POST /tools/strategies/{id}/publish` | token-gated write |
| `rollback_strategy` | `POST /tools/strategies/rollback` | token-gated write |

The MCP process would validate JSON Schema, inject authenticated actor identity, generate a
request ID, require an idempotency key for mutations, and forward to the Java service. It must
not receive Elasticsearch credentials. Approval tokens should be treated as secrets and passed
only for a single publish/rollback call.

Resource endpoints can expose current strategy, recent evaluation runs, and the API contract.
Prompts may help an agent form evidence-backed proposals, but prompts cannot bypass state
transitions. The REST service remains the authorization, idempotency and audit source of truth.

Production hardening would add OAuth/service identity, role separation for approve/publish,
rate limits, short-lived approval tokens and tamper-evident audit export. None is needed to
demonstrate the local workflow.

