# ADR 0003: Stable ESCI sampling and synthetic commerce attributes

- Status: Accepted
- Date: 2026-08-02

## Context

The official ESCI product parquet is large and has no prices, inventory, transaction or customer data. A laptop-sized subset must retain useful query judgments.

## Decision

Use only `product_locale=us`, `small_version=1`, and stable hash ordering under a configurable seed. Select up to `QUERY_LIMIT` queries and retain their full candidate judgments. Include those products before hash-filling to `PRODUCT_LIMIT`; fail clearly if the chosen query set would exceed the product cap.

Derive price, stock, coarse demo category, rating-like display value and popularity from SHA-256 of the product ID. Treat every cart, user, click, query-traffic event and order as simulation. Use local CSS placeholder art only.

## Consequences

The same source files, limits and seed produce byte-stable processed files. Offline metrics remain meaningful for the retained judgments. Simulated attributes are useful for feature demonstrations but have no Amazon transaction provenance.

