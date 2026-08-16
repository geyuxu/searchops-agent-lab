# SearchOps Agent Lab

An e-commerce search system built on the public **Amazon Shopping Queries dataset (ESCI)**, used to
answer one engineering question:

> **Can an LLM agent safely operate search relevance tuning?**

Search operations is a high-frequency, judgement-heavy, reversible-but-risky loop — detect poor
queries, diagnose causes, adjust synonyms and field weights, roll out, roll back. This project opens
that loop to an agent while constraining it with two things that do not depend on trusting the model:
a **structural permission boundary** and a **statistical promotion gate**.

---

## Headline result

On a 600-query held-out set, with LLM candidate reranking as the only change:

| Depth | NDCG@10 | Δ vs BM25 baseline | 95% CI | p | Gate |
|---|---|---|---|---|---|
| baseline (BM25, strategy v7) | 0.4720 | — | — | — | — |
| rerank N=20 | 0.5687 | +0.0968 | [+0.0796, +0.1144] | 0.0001 | PROMOTE |
| **rerank N=50** | **0.5926** | **+0.1207** | [+0.1006, +0.1416] | 0.0001 | PROMOTE |
| rerank N=100 | 0.6068 | +0.1349 | [+0.1133, +0.1570] | 0.0001 | PROMOTE |

Under the same evaluation, **LLM query rewriting produced no statistically significant gain**
(Δ+0.0025, p=0.5942, gate BLOCK). That contrast — not the single number — is the point: it was
predicted by a per-query failure diagnosis showing that 57.6% of total failures were *ranking*
problems rather than *retrieval* problems.

All statistics come from `agent/searchops_agent/eval/` (paired bootstrap, permutation test, Cliff's
delta, Benjamini–Hochberg correction). Raw artifacts are in `experiments/`.

### Honest limits

- The evaluation set is sparsely judged: roughly **12% of top-10 positions carry a human label**, and
  each query averages ~1.7 relevant (E/S) judgements. Retrieved-but-unjudged documents score 0, so a
  genuine improvement that surfaces unlabelled good products is scored as a regression.
- Minimum detectable NDCG@10 difference on the 600-query holdout is about 0.016.
- Reranking degraded on 10/600 queries (timeout / transport error); those fall back to the baseline
  ordering, so the reported effect is if anything a slight **under**estimate.
- Hosted models cannot be reproduced exactly across vendor weight updates. The model is pinned to a
  dated snapshot (`qwen3.7-flash-2026-07-15`) and recorded with each run.

---

## Architecture

```
                    ┌───────────────────────────────────────────┐
   Agent ──tools──▶ │  Tool Gateway  (safety-classed)           │
                    │  read · dry-run · governed write          │
                    │  ─────────────────────────────────────    │
                    │  approve · publish · rollback  ← human only│
                    └───────────────┬───────────────────────────┘
                                    │
   ┌────────────────────────────────▼─────────────────────────────┐
   │  Search service (Java 21 / Spring Boot)                      │
   │    strategy compiler → Elasticsearch (BM25)                  │
   │    ├─ optional query rewrite   ─┐                            │
   │    └─ optional candidate rerank ─┼─▶ AI adapter (FastAPI)    │
   │                                  │     LangChain providers   │
   │    fallback to BM25 on any failure, with a typed reason      │
   └──────────────────────────────────────────────────────────────┘
                                    │
   ┌────────────────────────────────▼─────────────────────────────┐
   │  Offline evaluation + promotion gate  (Python)               │
   │    per-query metrics → paired bootstrap / permutation        │
   │    → effect size → BH correction → fail-closed gate          │
   └──────────────────────────────────────────────────────────────┘
```

Three properties are worth calling out, because each replaces a promise with a mechanism:

**The agent cannot publish.** Its toolset is built from an allowlist filtered by safety class, so
`approve` / `publish` / `rollback` are not merely discouraged — they are absent from the registry.
A unit test asserts this.

**Degradation is typed, not silent.** When a model is unavailable, slow, or returns something
unusable, search still succeeds on BM25 — but the response carries *why*. Rewrite and rerank have
separate state machines, because one request can legitimately be "rewrite applied, rerank timed out",
and collapsing those into one boolean makes failures unattributable.

**Reranking cannot lose documents.** The merge works on candidate *indices*, not IDs: out-of-set IDs
are ignored, unmentioned candidates are appended in original order, duplicates collapse. The result
is provably a permutation of the input. If it ever is not, the request falls back to BM25 rather than
returning a silently shortened result set.

---

## Quick start

Requires Docker, JDK 21, Node 22+, Python 3.10+.

```bash
cd platform
make doctor        # check host tooling
make bootstrap     # install pinned dependencies
make data          # download + deterministically sample ESCI
make up            # start the full stack in containers
make seed          # load PostgreSQL and build the Elasticsearch index
make evaluate      # reproducible offline evaluation
```

Storefront on `:3000`, SearchOps console on `:3001`, search API on `:8080`.

For development, run stateful infrastructure in containers and the applications as host processes:

```bash
make infra-up      # postgres + redis + elasticsearch only
make dev-info      # prints how to start each application locally
```

Enabling a real model is documented in [`platform/docs/ai-handoff.md`](platform/docs/ai-handoff.md).
API keys are read indirectly — `AI_API_KEY_ENV` holds the *name* of an environment variable, never a
value — and no key is written to any file in this repository.

---

## Data boundary

Product text, brands, descriptions, search queries and relevance labels are **real public data** from
[amazon-science/esci-data](https://github.com/amazon-science/esci-data) (Apache-2.0).

Prices, inventory, categories, popularity, users, traffic, carts and orders are **deterministic
simulations** derived from a hash of the product ID. They are not Amazon transaction data and must
never be presented as such. Product artwork is generated locally; no Amazon image is hotlinked.

See [`platform/docs/data-provenance.md`](platform/docs/data-provenance.md).

---

## Repository layout

| Path | Contents |
|---|---|
| `platform/` | Search service (Java), commerce service, storefront and operations console (Next.js), AI adapter (FastAPI), ESCI data pipeline, Docker orchestration |
| `agent/` | Agent tool registry with safety classes, offline evaluation statistics, promotion gate, proposal loop |
| `experiments/` | Held-out split manifest, baselines, sweep logs, rerank and rewrite results |
| `baselines/` | Archived reference evaluation, checksummed |

---

## Licence

Code in this repository is released under the MIT Licence. The ESCI dataset retains its own
Apache-2.0 licence and is not redistributed here — `make data` downloads it from the official source.
