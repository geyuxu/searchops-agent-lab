# ADR 0001: Runtime and version baseline

- Status: Accepted
- Date: 2026-08-02

## Context

The system must be reproducible on Apple silicon and a 16 GiB-class development machine while using current compatible stable releases.

## Decision

- Node 24 LTS in containers; Medusa 2.18.0; Next.js 16.2.12; React 19.2.8; TypeScript 7.0.2; Playwright 1.62.1.
- Java 21 with Spring Boot 4.1.0 and Elasticsearch 9.4.2. The search service uses Elasticsearch's HTTP API through Spring's REST client so the Elasticsearch server version is explicit and client code is narrow.
- Python 3.13 with FastAPI and Pydantic versions locked by uv.
- PostgreSQL 17 and Redis 7.4 are single local instances shared by logically separated schemas/databases.
- Elasticsearch runs as one node with security disabled only in this local-development Compose profile and a 1 GiB fixed heap.

## Consequences

Host runtimes do not need to match the build matrix. Docker image digests can be added later for supply-chain hardening. Security-disabled Elasticsearch must never be exposed outside localhost or reused as a production manifest.

