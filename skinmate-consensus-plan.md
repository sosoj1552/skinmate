# skinmate — Consensus Implementation Plan (RALPLAN-DR / SHORT)

> **Status: pending approval.**
> Fresh-start plan derived solely from `.omc/specs/deep-interview-skinmate-conversational-cosmetics-rec.md`.
> **Deliberate fresh-start decision (authoritative):** this plan does NOT reuse, reconcile, or cross-check `db/schema.sql`, `cosmetics-recommendation-consensus-plan.md`, or `WBS.md`. All schema, dimensions, and constants below are decided independently here. Reviewer findings that asked to reconcile with `db/schema.sql` (Architect rec #2; Critic m1/m2/m3) are **intentionally dismissed** — see the Consensus Revision Log. Any prior artifact is treated as non-authoritative history.
> Mode: SHORT consensus. Round: PLANNER revised (post Architect + Critic review — both APPROVE-WITH-IMPROVEMENTS).

---

## 1. Requirements Summary

Build a multi-user **conversational cosmetics recommender** that fuses three context sources per user:

1. **Structured knowledge** (②): ingredient grade/effect/classification tables, products, product↔ingredient relations — loaded from real public data / crawling.
2. **Document embeddings** (③): prose skincare docs stored as pgvector embeddings for similarity search.
3. **Personal memory** (①): LLM-managed facts (add/update/delete/no-op + importance filter), prioritized by `effective_weight = base_weight × exp(-λ × days_since_last_seen)`.
4. **Relationship graph** (④): Apache AGE user + global subgraphs; **2+ hop traversal is the core reasoning** for rich rationale.

Cross-cutting:
- **⑤ Storage Sync**: writes to relational + graph + vector are **atomic in a single Postgres transaction**; any failure → full rollback. Memory writes are **SYNCHRONOUS** — performed within the turn (after the response is generated) in one transaction on one connection. **No queue / worker / drain / dead-letter** — single-Postgres atomicity is the entire mechanism; because the write commits before the turn ends, the next turn always sees it (no cross-turn staleness).
- **⑥ Chat/Recommendation**: **adaptive output** — concrete query → product names + rationale immediately; vague query → advice→funnel to final products.
- **Isolation**: `user_id` scoping everywhere. RLS on relational tables; AGE has no RLS → single choke-point function injecting `user_scope`. Cross-user leakage must test **0 rows**.
- **Hard constraint**: avoided **ingredients** guaranteed 0 occurrences (structured filter on relational `product_ingredients`). **Formulation (제형)** is best-effort via **embedding ranking ONLY** (spec R10/R11) — it is deliberately NOT a graph node, NOT a relational tag, and NOT a hard filter. Texture/제형 signal lives solely in `D_DOC` product-description embeddings.

**Locked non-goals**: no e-commerce/payment/inventory; no non-domain chat memory; no multi-factor weight model (single λ); no hard formulation guarantee; no distributed transaction/saga; **no async write queue/worker in v1** (synchronous atomic write — see ⑤).

---

## 2. RALPLAN-DR Summary (SHORT)

### Principles
1. **One transactional boundary — single connection, single transaction.** The atomic-rollback requirement (AC-S1) is only trivially satisfiable if all three stores share one transaction manager. Physical co-location is a hard design driver, not an optimization. **Pinned invariant:** the write path opens **ONE** Postgres connection, issues `BEGIN`, performs the relational INSERT/UPDATE, the pgvector INSERT/UPDATE, and the AGE `cypher()` write **on that same connection**, then `COMMIT`; any exception → `ROLLBACK`. **No autocommit statements anywhere in the write path.** This is the enforcement mechanism behind AC-S1 (see Step 6).
2. **Reads and memory writes are both synchronous within the turn.** The retrieval path (pgvector + AGE traversal + memory ranking) runs first and drives the response; the memory CRUD write then runs in the same request, after the response is generated, as one atomic transaction, before the turn returns. No background jobs, no drain gate — the write commits before the next turn can start, so retrieval always sees the latest memory (AC-S2 reframed as synchronous visibility).
3. **Structured facts are source of truth; graph is a derived projection.** `product_ingredients` (relational) is authoritative for the hard ingredient filter (AC-R2). AGE `CONTAINS` edges are a rebuildable projection for traversal (AC-G2), never the enforcement point.
4. **Isolation enforced at a choke point.** Every AGE query flows through one `user_scope`-injecting function; relational rows get RLS. Both are covered by a 0-row cross-user leakage test (AC-M5, AC-G3).
5. **Rationale must be grounded.** Recommendation reasoning cites actual graph paths + memory facts; no hallucinated justification (AC-R3).

### Decision Drivers (top 3)
1. **Atomic 3-store rollback (AC-S1)** — the single strongest driver; eliminates any multi-database topology.
2. **2+ hop traversal as core reasoning (AC-G2)** — mandates a real graph engine, not ad-hoc relational joins, for rich multi-hop rationale.
3. **Full multi-user isolation, 0-row leakage (AC-M5/AC-G3)** — drives RLS + AGE choke-point design and its test backstop.

### Viable Options

**Option A — Single Postgres (pgvector + AGE) [CHOSEN]**
All three stores in one instance; atomicity = one local `BEGIN…COMMIT`.
- Pros: Native single-transaction atomicity (AC-S1 becomes trivial); one connection/pool/backup; graph + vector + relational join in one query planner; no cross-store 2PC.
- Cons: AGE + pgvector coexistence is less battle-tested than either alone; AGE multi-hop performance can degrade at scale; AGE lacks RLS (needs app-level choke point); single instance = single scaling/availability unit.

**Option B — Separate graph DB (Neo4j) + vector DB (pgvector/Qdrant) + relational Postgres [REJECTED]**
- Pros: Best-in-class graph traversal (Neo4j Cypher maturity); independent scaling per store.
- Cons: **Atomic rollback across 3 databases requires a distributed saga / 2PC** — directly violates the locked constraint (R9, spec lines 42/58) and inflates AC-S1 from a one-liner to a whole subsystem. **Invalidation rationale**: the user explicitly locked "single Postgres single transaction, no saga." This option is disqualified by a hard constraint, not merely by cost.

**Option C — Postgres relational + pgvector, NO graph DB (relational adjacency only) [REJECTED, kept as fallback]**
- Pros: Simplest; recursive CTEs can do multi-hop; no AGE maturity risk.
- Cons: Round 4 initially favored this, but **Round 6 the user reversed it** — 2+ hop traversal for rich rationale is the declared core reasoning method (spec line 179). **Invalidation rationale**: rejected as the *primary* architecture because the user made the graph DB a locked requirement. **For v1, no second traversal engine is built.** A materialized relational-adjacency path is retained only as a **benchmark-only** measurement item and, if ever needed, strictly as a **read-through cache of AGE-computed paths** — never an independent traversal engine (see Risks R2). This keeps "graph is core reasoning" intact.

**Conclusion**: Option A is the only option consistent with all locked constraints (atomic single-transaction + mandatory graph DB). B and C each violate a distinct hard requirement.

---

## 3. Module / Directory Layout

**Recommended implementation language: Python** (rationale: strongest ecosystem for LLM orchestration + embeddings + `psycopg` with AGE/pgvector; crawling via `httpx`/`scrapy`). The synchronous atomic write is a plain `psycopg` transaction — no worker/broker library needed. Language-agnostic logical structure below.

```
skinmate/
├── db/                          # (exists) Postgres image w/ AGE + pgvector
│   ├── initdb/                  # CREATE EXTENSION vector, age; roles
│   └── migrations/              # ordered SQL migrations (schema, RLS, AGE graphs, funcs)
├── src/skinmate/
│   ├── knowledge/               # ② Structured Knowledge
│   │   ├── models.py            #   ingredients, products, product_ingredients, concerns…
│   │   └── repository.py        #   ingredient→products lookup, hard avoid-ingredient filter
│   ├── documents/               # ③ Document Embeddings (RAG)
│   │   ├── embed.py             #   embedding model wrapper (doc space)
│   │   └── search.py            #   pgvector similarity over docs + product descriptions
│   ├── memory/                  # ① Memory
│   │   ├── crud.py              #   LLM add/update/delete/no-op + importance filter
│   │   ├── weight.py            #   effective_weight = base_weight·exp(-λ·Δdays)
│   │   └── repository.py        #   user_id-scoped fact store (RLS)
│   ├── graph/                   # ④ Relationship Graph (AGE)
│   │   ├── ontology.py          #   node/edge labels (NO Formulation node)
│   │   ├── projection.py        #   deterministic relational→AGE rebuild: CONTAINS (product_ingredients),
│   │   │                        #   AFFECTS (seasons/season_concerns), TREATS/HELPS (ingredient rels),
│   │   │                        #   AND per-user AVOIDS/PREFERS from memories (user-scoped)
│   │   ├── choke.py             #   single user_scope-injecting query function
│   │   └── traverse.py          #   2+ hop Cypher templates + path→rationale extraction
│   ├── write/                   # ⑤ Storage Sync (synchronous atomic write)
│   │   └── writer.py            #   one-connection BEGIN→(relational+vector+AGE)→COMMIT/ROLLBACK
│   ├── chat/                    # ⑥ Chat/Recommendation
│   │   ├── orchestrator.py      #   adaptive concrete-vs-vague routing + funnel state
│   │   ├── retrieve.py          #   fuse pgvector + AGE traversal + memory ranking
│   │   └── rationale.py         #   grounded reasoning (graph path + memory citations)
│   ├── llm/                     #   provider abstraction (default Claude API)
│   └── app/                     #   API surface (FastAPI): /chat, /admin
├── ingest/                      # crawlers + loaders (ingredients, products, docs)
│   ├── sources/                 #   one module per public source (robots/ToS/rate-limit)
│   └── normalize.py             #   INCI canonical key, dedup, source metadata
└── eval/                        # AC test harness (AC-D/M/S/G/R)
```

Existing repo dirs (`chat/`, `ingest/`, `memory/`, `retrieval/`, `contracts/`, `eval/`, `scripts/`) map onto this and can be reused as landing spots; `db/` and `docker-compose.yml` are already present.

---

## 4. Data Model

> ⚠️ **SUPERSEDED (2026-07-07):** 이 절의 테이블 목록은 2인 재설계로 갱신되었다. 스키마의 **단일 진실원은 [docs/DATA-MODEL.md](docs/DATA-MODEL.md)** 이며 구현은 `db/migrations/`이다. 주요 변경: `seasons`·`season_concerns`·`concerns`·`conversations`·`data_sources` 테이블 삭제(계절·고민은 개인 맥락 memories로, 출처는 각 행 source_meta로); `TREATS/HELPS` 등 지식 엣지는 테이블 없이 **그래프 네이티브**; 서버 **stateless**. 아래 원문은 히스토리로 보존.

### Decided embedding constants (v1 — independently chosen, not from schema.sql)
- **`D_DOC` = 1024**, multilingual model (KO+EN). Default: **BAAI/bge-m3** (1024-dim, strong KO+EN, single model for both product prose and docs). *Rationale: one multilingual model avoids a second embedding pipeline and covers Korean skincare prose + English INCI docs.*
- **`D_ING` = NOT USED in v1.** Ingredient semantics are carried by structured relational columns (grade/function/classification) plus the AGE graph; formulation/texture ranking rides on `D_DOC` product-description embeddings. *Rationale: no query path in the ACs requires ingredient-level vector similarity; dropping D_ING removes an ingestion + storage burden with zero AC cost. Re-add later only if an ingredient-similarity feature emerges.*
- **`embedding_model_id`** column is stored on every vectorized row (see below) to permit a future re-embed migration. *Rationale: embedding models version; without a per-vector model id, a model swap silently mixes incomparable vector spaces.*

### Relational (source of truth)
- **users**(user_id PK, skin_type, created_at)
- **concerns**(concern_id, name)  — 건조·민감·트러블
- **seasons**(season_id PK, name)  — 봄·여름·가을·겨울. **SOURCE OF TRUTH** for the seasonal signal.
- **season_concerns**(season_id, concern_id, relation)  — e.g. (가을, 건조, AGGRAVATES). **SOURCE OF TRUTH** for the `(:Season)-[:AFFECTS]->(:Concern)` graph edge; that edge is REBUILT from this table by the projection job, never hand-authored. Gives the "가을→건조" scenario (AC-R4) a real data path.
- **ingredients**(ingredient_id, inci_key UNIQUE, name_ko, name_en, grade, function, classification, source_meta) — **no embedding column in v1** (D_ING dropped).
- **products**(product_id, name, brand, category, description, embedding vector(1024), embedding_model_id, source_meta)
- **product_ingredients**(product_id, ingredient_id) — **SOURCE OF TRUTH** for containment; drives the hard avoid-ingredient filter (AC-R2) and is projected into AGE as `CONTAINS`.
- **documents**(doc_id, content, embedding vector(1024), embedding_model_id, source, crawled_at) — skincare prose RAG.
- **memories**(memory_id, user_id, content, fact_type, slot_key, base_weight, frequency, last_seen, deleted_at NULL) — **RLS by user_id**; soft-delete + audit. `fact_type` marks graph-projectable facts (e.g. `avoid_ingredient`, `prefer_ingredient`) that become per-user AGE edges (see Memory→Graph bridge).
- **memory_audit**(memory_id, op, old_val, new_val, at) — CRUD audit trail.
- *(no `write_jobs` table — memory writes are synchronous; the async job ledger was removed.)*
- **conversations**(conv_id, user_id, turns JSONB, funnel_state).
- **data_sources**(source_id, url, kind, crawled_at, robots_ok).

### Embedding-space separation (explicit)
Docs/products use `D_DOC=1024` (semantic prose space). `D_ING` is unused in v1. **Memory facts are NOT vector-searched by default** — they are retrieved by `user_id` + `effective_weight` ranking, and only similarity-matched *within a user's own facts* during CRUD dedup (a separate, per-user memory space). Doc space and memory space are kept distinct to prevent cross-contaminating RAG similarity with personal-fact similarity.

### AGE graph ontology (derived / traversal layer)
- **Nodes**: `User`, `Ingredient`, `Product`, `Concern`, `Season`, `Brand`. **No `Formulation` node** — texture/제형 is embedding-only (spec R10/R11), never a structured node or relational tag.
- **Edges (global knowledge, all derived by projection)**: `(:Season)-[:AFFECTS]->(:Concern)` **(from `seasons`/`season_concerns`)**, `(:Ingredient)-[:TREATS|AGGRAVATES]->(:Concern)`, `(:Ingredient)-[:HELPS|CONFLICTS]->(:Ingredient)`, `(:Product)-[:CONTAINS]->(:Ingredient)` **(from `product_ingredients`)**.
- **Edges (per-user, `user_scope` property, derived from `memories`)**: `(:User)-[:HAS_CONCERN]->(:Concern)`, `(:User)-[:PREFERS|AVOIDS {user_scope}]->(:Ingredient|Brand)`. (No Formulation target — preference for texture is expressed only via embedding ranking, not an AVOIDS/PREFERS edge.)
- **Core traversal (AC-G2)**: sensitivity → AVOIDS ingredient → CONTAINS products (exclude) → alternative ingredient (HELPS/TREATS) → alternative products. The traversed path is captured and surfaced as rationale.
- **Memory→Graph bridge (the ①→④ link).** A relational memory fact such as "레티놀 안 맞음" is stored in `memories` with `fact_type='avoid_ingredient'` and a resolved `ingredient_id`. **The projection — running inside the SAME synchronous atomic write transaction as the memory write (Step 6) — projects that row into a per-user AGE edge** `(:User {user_scope})-[:AVOIDS]->(:Ingredient)`. The projection ALWAYS routes through the `choke.py` `user_scope` injector, so the resulting edge is stamped with the owning `user_id` and can only be traversed under that scope. AC-G2 traversal then consumes this edge; AC-G3 proves it stays user-scoped (0 cross-user rows). Deleting/soft-deleting the memory removes the corresponding edge in the same transaction.
- **Projection rule**: ALL AGE edges (global CONTAINS/AFFECTS/TREATS/HELPS **and** per-user AVOIDS/PREFERS) are rebuilt from relational tables by the deterministic projection job (Step 4). No AGE edge is ever hand-authored. The hard ingredient filter (AC-R2) is enforced against relational `product_ingredients`, never against AGE.

---

## 5. Implementation Steps (ordered)

1. **DB foundation + migrations** — `db/migrations/`, `db/initdb/`. Create `vector` + `age` extensions, all relational tables (incl. `seasons`, `season_concerns`), RLS policies on `memories`, and the AGE graph(s). Satisfies groundwork for AC-S1, AC-M5, AC-G1.
2. **Ingestion pipeline + data-adequacy gate** — `ingest/sources/`, `ingest/normalize.py`. **Two pluggable crawl sources (decided):** (1) **coos.kr** → ingredient canonical key (INCI where present, else Korean name) + Korean names/aliases + grade/function; (2) **Paula's Choice Beautypedia (paulaschoice.co.kr)** → ingredient explanatory prose for `D_DOC` docs + product pages for formulation/texture description. Neither site exposes robots.txt → crawl politely (rate-limit ~1–2 req/s, cache raw responses, explicit User-Agent, store `source` + `crawled_at`; ToS/copyright still apply — non-commercial use, revisit before any commercialization). INCI canonical-key normalization + dedup; embed docs/products into `D_DOC` (1024). **Data-adequacy gate:** validate formulation/texture tokens (에멀전/오일/끈적임…) present in ≥60% of product descriptions; if under gate, backfill with a small hand-curated product set. **Seasonal seed:** `season_concerns` is **hand-curated** (~10–20 rows; no clean structured source exists). Satisfies **AC-D1, AC-D2**.
3. **Structured knowledge + hard filter** — `src/skinmate/knowledge/`. Ingredient→products lookup and the avoid-ingredient structured filter over `product_ingredients`. Satisfies **AC-D2, AC-R2**.
4. **Graph projection + traversal + choke point** — `src/skinmate/graph/`. Deterministic projection job rebuilding ALL AGE edges from relational tables: `CONTAINS` (product_ingredients), `AFFECTS` (seasons/season_concerns), ingredient relations, **and per-user `AVOIDS`/`PREFERS` from `memories` via the Memory→Graph bridge — always routed through `choke.py` so edges are user-scoped**. `choke.py` `user_scope` injector, 2+ hop Cypher templates, path→rationale extractor. Satisfies **AC-G1, AC-G2, AC-G3**.
5. **Memory subsystem** — `src/skinmate/memory/`. LLM CRUD (add/update/delete/no-op) + importance filter; `effective_weight` computation; RLS-scoped repo + audit. Satisfies **AC-M1, AC-M2, AC-M3, AC-M5**.
6. **Storage write (synchronous atomic)** — `src/skinmate/write/`. After the response is generated, the request path executes the memory CRUD **as one single-connection transaction** (pinned invariant §2/Principle 1): open ONE connection → `BEGIN` → relational INSERT/UPDATE + pgvector write + AGE `cypher()` write (incl. the memory→graph AVOIDS/PREFERS projection) → `COMMIT`; any exception → `ROLLBACK` (the turn surfaces a write error, but the response was already produced from committed state). **No queue, worker, drain, or dead-letter.** Same-user turns are naturally serialized by request ordering; a per-user advisory lock guards the rare concurrent-request case. **AC-S1 scope: the atomic 3-store write covers the per-turn MEMORY write path only.** Bulk ingestion + the global-edge graph projection (Step 2/4) are a separate offline batch path with their own idempotent-rebuild semantics. Satisfies **AC-S1, AC-S2**.
7. **Chat/recommendation orchestrator** — `src/skinmate/chat/`, `src/skinmate/app/`. Adaptive concrete-vs-vague routing + funnel; retrieval fusion (pgvector + AGE + memory ranking); grounded rationale; formulation soft-ranking (emulsion-over-oil via `D_DOC`). Depends on 3–6. Satisfies **AC-R1, AC-R3, AC-R4, AC-F1, AC-M4**.
8. **Eval harness** — `eval/`. Automated tests for every AC (see §7), including cross-user 0-row leakage, hard-filter 0-occurrence checks, formulation soft-ranking, and the recency write→read loop.

---

## 6. Acceptance Criteria (operationalized)

Inherited from spec; vague ones given concrete thresholds.

| ID | Criterion | Operationalized threshold |
|----|-----------|---------------------------|
| AC-D1 | Real data loaded; pgvector returns top similar; **data-adequacy gate** | ≥1 real source ingested; top-k (k=5) similarity query returns non-empty, source-attributed results; **AND formulation/texture tokens (에멀전/오일/끈적임/제형…) present in ≥60% of product descriptions; AND a seasonal signal source or curated `season_concerns` seed exists (non-empty)** |
| AC-D2 | ingredient → products lookup | Known ingredient returns its exact product set from `product_ingredients` |
| AC-M1 | add/update/delete/no-op correct per case **+ non-destructive coexistence** | 4 scripted cases each produce the correct op and store state; **PLUS negative case: a still-true fact must NOT be deleted or overwritten when a new NON-conflicting mention arrives — assert both facts coexist afterward** |
| AC-M2 | frequent+recent facts rank above old+rare | Given fixed λ=0.05/day, ranking order matches `effective_weight` for a seeded fixture |
| AC-M3 | importance classification | On a labeled set (≥20 utterances, trivia vs domain), **precision ≥ 0.85 for "store", recall ≥ 0.85 for domain facts** |
| AC-M4 | memory reflected in output | Same query with vs without memory yields **materially different** recommendation sets (≥1 differing product AND differing rationale) |
| AC-M5 | user_id isolation | Cross-user memory read returns **exactly 0 rows** |
| AC-M6 | recency loop (write→read) | Mention fact F (written synchronously in that turn's commit); on the next query assert F's `effective_weight` rank **rose** relative to an un-mentioned peer fact of equal base_weight |
| AC-S1 | atomic single-tx memory write | **Scope: per-turn memory write path only** (ingestion/global-projection is a separate batch, excluded). Fault injected **AFTER the AGE `cypher()` write but BEFORE `COMMIT`** → assert **0 partial rows** in all three stores: relational rows written earlier in the same tx **also roll back**, AGE node/edge count unchanged, vector count unchanged |
| AC-S2 | synchronous write visibility | The memory write **commits before the turn returns**; a fact written in turn N is visible to retrieval in turn N+1 with no drain gate and no staleness window |
| AC-F1 | formulation soft-ranking | For a user preferring emulsion + avoiding oil, on a fixed concern query over a seeded texture-labeled fixture, **emulsion-described products rank strictly above equivalent oil-described products in ≥80% of paired comparisons** (soft, not a hard filter) |
| AC-G1 | core relations traversable via Cypher | Seeded relations (incl. Season→Concern from projection) queryable through AGE choke function |
| AC-G2 | ≥2-hop traversal drives rec + path shown | A rec is produced from a ≥2-hop path (incl. per-user `AVOIDS` edges from the memory→graph bridge) AND the path is emitted in the rationale |
| AC-G3 | subgraph isolation | Cross-user subgraph traversal returns **0 rows** |
| AC-R1 | adaptive output | Concrete query → immediate products+rationale; vague query → advice then ≥1 funnel narrowing to final products |
| AC-R2 | hard ingredient constraint | Over a randomized rec suite, avoided ingredient appears in **0** recommended products; formulation best-effort (ranked, not guaranteed) |
| AC-R3 | grounded rationale | Every rationale claim maps to an actual graph path or stored memory; **0 unsupported claims** on the audited sample |
| AC-R4 | canonical full-sync scenario (operationalized) | Scenario "가을이 되니 건조… 끈적한 제형 싫어 오일 아님, 에멀전인데 보습 확실" passes ALL sub-assertions: **(a)** emitted rationale contains a **season→concern path node** (가을→건조 via AFFECTS); **(b)** **0** avoided-ingredient products in the result (reuses AC-R2); **(c)** emulsion-described products **outrank** oil-described products for the same concern match (reuses AC-F1); **(d)** **≥1 recalled memory fact** appears in the rationale |

**Added budget**: sync read-path latency **p95 ≤ 2.5 s** end-to-end (pgvector + AGE 2-hop + memory ranking + LLM generation), with the retrieval (non-LLM) portion **p95 ≤ 400 ms** on the seed dataset.

---

## 7. Risks and Mitigations

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | **AGE + pgvector coexistence maturity** — both in one Postgres is less proven than each alone (version/build conflicts, `shared_preload_libraries` for AGE). | Pin exact Postgres + AGE + pgvector versions; CI smoke test (already scaffolded) that creates both extensions and runs a joined query; isolate AGE usage behind `graph/` so a version bump is contained. |
| R2 | **AGE multi-hop traversal performance** at scale. | **v1 = BENCHMARK ONLY.** Measure AGE p95 on the 2-hop rationale query against the latency budget. **Do NOT build a second traversal engine unless the budget is actually breached.** If ever needed, the fallback is strictly a **read-through cache of AGE-computed paths** (not an independent relational-adjacency engine), so "graph is core reasoning" (Principle 3) stays intact. |
| R3 | **Synchronous-write response latency** — memory CRUD (LLM extraction) adds ~1–3 s to the turn because it runs before the turn returns. | Run the write **after** the response is generated so answer quality/first-token latency is unaffected; keep the write behind `write/writer.py` so it can be promoted to an async queue later if latency becomes unacceptable; cross-turn staleness is **eliminated** (write commits before next turn). |
| R4 | **AGE isolation lacks a DB backstop** (no RLS). | Single choke-point function is the ONLY path to AGE; forbid raw `cypher()` calls in app code (lint/grep guard); back it with the AC-G3 0-row leakage test in CI as the enforcement backstop. |
| R5 | **LLM CRUD mis-delete** — model wrongly deletes a valid fact. | **Soft-delete + audit log** (never hard-delete); require importance/confidence threshold for delete ops; delete is reversible; AC-M1 scripted cases guard regressions. |
| R6 | **Crawling ToS/legal single point of failure** — one source blocks/changes and ingestion breaks or becomes non-compliant. | Source abstraction (`ingest/sources/*`) so ≥2 sources are pluggable; honor robots/ToS/rate-limit + cache raw responses with `crawled_at`/attribution in `data_sources`; ingestion failure of one source must not break the read path (system serves last-ingested cache). |
| R7 | **Embedding-model version lock-in** — swapping the embedding model silently mixes incomparable vector spaces, corrupting RAG similarity. | Store `embedding_model_id` on every vectorized row (`products`, `documents`); a model change is a planned **re-embed migration** (re-embed all rows to the new model id, then cut over) — never a mixed-space in-place swap; queries assert a single active model id. |

---

## 8. Verification Steps (per AC)

- **AC-D1/D2**: run ingestion against a real source; assert non-empty top-k pgvector results with source attribution; assert ingredient→product set equality; **assert formulation/texture-token coverage ≥60% and non-empty `season_concerns` seed (data-adequacy gate).**
- **AC-M1**: 4 scripted conversations (new/changed/retracted/duplicate) → assert resulting op and store state; **PLUS negative case: fire a non-conflicting new mention and assert the prior still-true fact is neither deleted nor overwritten (both coexist).**
- **AC-M2**: seed facts with controlled `last_seen`/`frequency`; assert ranking equals `effective_weight` order for fixed λ=0.05/day.
- **AC-M3**: labeled utterance set; compute store-precision / domain-recall ≥ 0.85.
- **AC-M4**: identical query for a memory-rich vs empty user → assert ≥1 differing product and differing rationale.
- **AC-M5 / AC-G3**: seed 2 users; attempt cross-user memory read and cross-user AGE traversal → assert exactly 0 rows (CI gate).
- **AC-M6**: mention fact F (synchronously committed that turn) → next query → assert F's `effective_weight` rank rose vs an un-mentioned equal-base_weight peer (recency loop).
- **AC-S1**: inject fault **after the AGE `cypher()` write, before COMMIT**; assert relational/AGE/vector counts all unchanged — including that relational rows written earlier in the same tx rolled back (no partial rows). Scoped to the per-turn memory write path only.
- **AC-S2**: assert the memory write commits before the turn returns; assert a fact written in turn N is retrievable in turn N+1 (no staleness window).
- **AC-F1**: seeded texture-labeled fixture; user prefers emulsion + avoids oil; fixed concern query → assert emulsion products rank strictly above equivalent oil products in ≥80% of paired comparisons.
- **AC-G1/G2**: seed relations (incl. Season→Concern via projection and a per-user AVOIDS edge from the memory→graph bridge); run choke-function Cypher; assert ≥2-hop path produced AND emitted in rationale.
- **AC-R1**: two fixtures (concrete, vague); assert immediate products+rationale vs advice→funnel→products.
- **AC-R2**: randomized rec suite with avoided ingredients; assert 0 occurrences in recommended products.
- **AC-R3**: audit rationale sample; assert every claim maps to a real graph path or memory fact (0 unsupported).
- **AC-R4**: run the canonical "가을이 되니 건조… 끈적한 제형 싫어 오일 아님, 에멀전인데 보습 확실" scenario end-to-end; assert all four sub-assertions (a) season→concern path node, (b) 0 avoided-ingredient products, (c) emulsion outranks oil, (d) ≥1 recalled memory fact in rationale.
- **Latency budget**: load-test retrieval + end-to-end; assert p95 ≤ 400 ms (retrieval) and ≤ 2.5 s (e2e). **Also benchmark AGE 2-hop p95 (R2 benchmark-only gate).**

---

## Resolved Decisions (previously open — now decided independently, NOT deferred to schema.sql)
- **Embedding model + dimension (`D_DOC`)**: **BAAI/bge-m3, 1024-dim**, multilingual KO+EN. *One model for product prose + docs avoids a second pipeline.*
- **Ingredient embeddings (`D_ING`)**: **NOT used in v1.** *No AC needs ingredient-level vector similarity; structured columns + graph cover it.*
- **λ initial value**: **0.05 per day** (≈14-day half-life). *Sensible recency decay for skincare facts; tunable against AC-M2.*
- **`embedding_model_id`** stored per vector row for a future re-embed migration (R7).
- **Data sources (crawl)**: **coos.kr** (ingredient canonical + Korean names) + **Paula's Choice Beautypedia / paulaschoice.co.kr** (ingredient docs + product formulation prose). No robots.txt → polite rate-limited cached crawl (ToS/copyright still apply; non-commercial). `season_concerns` **hand-curated**. *Rationale: two pluggable sources cover ingredient canonicalization + descriptive prose; no clean structured seasonal source exists so it is seeded by hand.*
- **Memory write mode**: **SYNCHRONOUS** atomic single-transaction write — no `write_jobs` queue/worker/drain. *Rationale: single-Postgres already guarantees 3-store atomicity, so a queue only bought async delivery, which v1 doesn't need; accepts ~1–3 s added turn latency, eliminates cross-turn staleness, promotable to async later behind `write/writer.py`.*

## Remaining Open Questions (deferred to execution, tracked in open-questions.md)
- *(none blocking)* — both prior open questions resolved: data sources decided (coos.kr + Paula's Choice), write mode decided (synchronous, no worker). One execution-time check remains: confirm Paula's Choice product pages actually clear the AC-D1 ≥60% texture-token gate; if under, hand-curate the shortfall.

---

## 9. ADR — Single Postgres (AGE + pgvector), Fresh-Start

- **Decision**: Build v1 on a single Postgres instance with Apache AGE (graph) + pgvector (embeddings) + native relational, on a fresh schema authored in this plan.
- **Drivers**: (1) atomic 3-store rollback (AC-S1); (2) 2+ hop traversal as core reasoning (AC-G2); (3) full multi-user 0-row isolation (AC-M5/AC-G3).
- **Alternatives considered**: (B) Neo4j + separate vector DB + relational — **rejected**: atomic rollback across 3 DBs needs a saga/2PC, violating the locked "single Postgres, single transaction, no saga" constraint. (C) Relational-adjacency only, no graph DB — **rejected as primary**: user locked graph traversal as core reasoning; adjacency retained only as a v1 benchmark / read-through cache, never a second engine.
- **Why chosen**: Option A is the only topology consistent with ALL locked constraints; atomicity becomes a local `BEGIN…COMMIT` on one connection.
- **Consequences**: single scaling/availability unit; AGE+pgvector coexistence maturity risk (R1); AGE has no RLS → app-level choke point mandatory (R4). Formulation is embedding-only (no graph node) per R10/R11. **Memory writes are synchronous** (no async queue) — simpler and staleness-free, at the cost of ~1–3 s turn latency; promotable to async later.
- **Follow-ups**: benchmark AGE 2-hop p95 (R2); finalize crawl source under the AC-D1 prose gate (R6); plan embedding re-embed migration path (R7).

---

## Consensus Revision Log

Changes made in response to Architect + Critic (both APPROVE-WITH-IMPROVEMENTS) and the user's authoritative fresh-start decision:

- **Fresh-start (user, authoritative)**: Reviewer findings to reconcile with `db/schema.sql` (Architect rec #2; Critic m1/m2/m3) are **intentionally dismissed** — this is a deliberate pure fresh start, not an oversight (noted in header). The previously-open questions were instead **decided independently here** (D_DOC=bge-m3/1024, D_ING dropped, λ=0.05/day), NOT deferred to schema.sql.
- **C1 (season source of truth)**: Added relational `seasons` + `season_concerns`; the `(:Season)-[:AFFECTS]->(:Concern)` edge is now REBUILT by the projection job (Step 4), never hand-authored. Gives 가을→건조 a real data path.
- **C1 (formulation dualism)**: **Removed `Formulation` as an AGE node entirely** and as a PREFERS/AVOIDS target. Texture/제형 lives only in `D_DOC` embeddings (spec R10/R11). Fixed §1, §3 layout, §4 ontology.
- **C2 (AC-R4 operationalized)**: AC-R4 now has 4 checkable sub-assertions (season→concern path node; 0 avoided-ingredient products; emulsion>oil ranking; ≥1 recalled memory fact in rationale).
- **Memory→Graph bridge**: Specified how a relational memory fact (`fact_type='avoid_ingredient'`) becomes a per-user `(:User)-[:AVOIDS]->(:Ingredient)` AGE edge inside the Step-4 projection, routed through the choke point (user-scoped), written in the same atomic tx as the memory (Step 6).
- **TX invariant (Architect major)**: Pinned single-connection/single-transaction invariant in Principle 1 + Step 6. AC-S1 now injects the fault AFTER the AGE write but BEFORE COMMIT and asserts earlier relational rows also roll back. Clarified AC-S1 scope = per-turn memory write only (ingestion/global projection is a separate batch).
- **M1 (AC-F1)**: Added formulation soft-ranking AC (emulsion > oil in ≥80% of paired comparisons).
- **M2 (data adequacy)**: Added AC-D1 gate (≥60% descriptive-token coverage + non-empty seasonal seed) and a source-selection gate on descriptive prose (Step 2).
- **M3 (recency loop)**: Added AC-M6 integration test chaining write→read, asserting rank rise. *(Originally write→drain→read; the drain step was dropped in the post-approval sync revision below.)*
- **M4 (CRUD negative + embedding lock-in)**: Added AC-M1 non-destructive-coexistence negative case; added R7 (embedding versioning) + `embedding_model_id` per vector.
- **Over-engineering cuts**: R2 reduced to benchmark-only (read-through cache if ever needed, not a second engine); `graph_relation_proposals` hybrid-expansion approval flow deferred out of v1 (not referenced by any AC).

### Post-approval revision (user decisions, 2026-07-06)
- **Data sources decided (R6 resolved)**: crawl **coos.kr** (ingredient canonical + Korean names) + **Paula's Choice Beautypedia** (ingredient docs + product formulation prose); no robots.txt → polite rate-limited cached crawl; `season_concerns` hand-curated. Updated §1⑤ context, Step 2, Resolved Decisions.
- **Memory write mode → SYNCHRONOUS**: removed the entire async subsystem — `write_jobs` table, per-user queue, worker, drain, dead-letter. The single-Postgres transaction now performs the atomic 3-store write inline (after response generation). Reframed §1⑤, Principle 1 (write path, not worker), Principle 2 (reads+writes both sync), §3 layout (`sync/`→`write/writer.py`), §4 (dropped `write_jobs`), Step 6, AC-S2 (sync visibility), AC-M6 (no drain), R3 (now response-latency, not staleness), ADR consequences. *Rationale: single-Postgres already guarantees 3-store atomicity, so the queue only existed for async delivery — which v1 doesn't need. Promotable to async later behind `write/writer.py`.*
