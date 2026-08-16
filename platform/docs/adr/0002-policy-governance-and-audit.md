# ADR 0002: Query-time policy compilation with governed PostgreSQL state

- Status: Accepted
- Date: 2026-08-02

## Context

Policies must be previewable, auditable, reversible and able to affect Elasticsearch without allowing users or agents to mutate it directly.

## Decision

Store policy documents and lifecycle state in PostgreSQL. Compile the current published document into each Elasticsearch request. Require an `Idempotency-Key` for every mutation. Approval generates a one-time version-bound token; publish and rollback validate that token and status. Insert append-only audit records in the same transaction as policy transitions.

Supported fields are synonyms, rewrite rules, pinned and blocked product IDs, brand boosts, field weights and a minimum score.

## Consequences

Policy changes become visible without index downtime. Very large synonym sets would eventually need analyzer-level versioned indices, but query-time expansion is safer and sufficient for the local data scale.

