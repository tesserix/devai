# Agentic Memory — Implementation Plan

DevAI's memory subsystem is architecturally pluggable (the `adapters.memory`
family) but functionally a keyword store with almost no writers. This plan
upgrades it into a real agentic memory: semantic recall backed by embeddings,
a closed learn loop (every run teaches future runs), observable operations,
and a managed lifecycle (TTL / decay / dedup) — without breaking the adapter
contract or adding a mandatory external vendor.

---

## 1. Current state (audited 2026-06-11)

### What exists and works

| Piece | Where | Status |
|---|---|---|
| `MemoryAdapter` ABC (`remember/recall/semantic_search/forget`) | `src/devai/adapters/memory/base.py` | ✓ clean |
| Factory + 6 providers (noop, redis, pgvector, mem0, zep, hondo) | `src/devai/adapters/memory/factory.py` | ✓ graceful degrade |
| `agent_memories` table (vector(1536), HNSW index, soft delete) | `db/migrations/0001_initial_schema.up.sql:116` | ✓ schema ready |
| `memory_injection` stage → `memory_context` → agent prompts | `pipeline/stages/lifecycle.py:306`, consumed by senior_developer, security_expert, qa_tester, staff_reviewer, supervisor, orchestrator | ✓ read path live |
| `sre_learn` stage writes one episodic record per SRE sweep | `pipeline/stages/sre.py:267` | ✓ write path (SRE only) |
| Contract tests across providers | `tests/unit/test_memory_adapters.py` | ✓ |

### Gaps (ranked by impact)

1. **Embeddings are never generated.** Nothing sets `settings.memory_embedder`
   (the only reference is a `getattr` in `factory.py`), so the pgvector
   adapter silently degrades to `ILIKE` keyword matching. The 1536-dim
   column and HNSW index are dead weight. "semantic_search" is semantic in
   name only for redis/pgvector/noop.
2. **ALM never learns.** Only `sre_learn` writes memories. The ALM pipeline
   reads memories that almost nothing produces.
3. **Filter pushdown broken.** `PgVectorMemoryAdapter.semantic_search()`
   fetches k rows then filters agent/memory_type in Python — filters
   silently shrink results below k.
4. **Local sandbox runs `DEVAI_MEMORY_PROVIDER=noop`** (`k8s/chart/values.yaml`)
   — memory is never exercised in dev.
5. **Zero observability.** No metrics on remember/recall/hit-rate; `/readyz`
   doesn't surface memory health.
6. **Two memory systems live concurrently.** Legacy
   `devai.services.memory.AgentMemory` (Redis, ULID, manual index sets) is
   still imported directly by `chat/agent.py`, `graph/orchestrator.py`,
   `agents/db_engineer.py`, `agents/tech_detector.py`.
7. **No lifecycle.** `expires_at` never enforced, `decay_old_memories()`
   never called, no dedup — the corpus only accretes noise.
8. **Chat history is ephemeral** (in-process dict, lost on restart, never
   distilled into memory).

---

## 2. Provider decision — "what's the best memory tool?"

**Recommendation: pgvector as the production default, with real embeddings
wired through the LLM adapter family. mem0 stays the supported premium
option; redis stays the zero-Postgres fallback; zep/hondo remain optional.**

Rationale, given DevAI's constraints (self-hosted GKE, multi-tenant ambition,
adapter pattern, no new mandatory vendors):

| Option | Verdict | Why |
|---|---|---|
| **pgvector (own Postgres)** | **Default** | Already deployed (schema, HNSW index, bootstrap CronJob). Zero new infra, zero vendor risk, data stays in-cluster, SQL-governable (TTL/decay/dedup are plain queries), multi-tenant via existing repo/agent scoping. Only missing piece is the embedder — one config block. |
| **mem0 (OSS / cloud)** | Optional upgrade | Best-in-class extraction (LLM distills facts before storing, auto-dedup, graph memory). Worth offering for tenants that want it; self-hostable. But it adds an external service + its own LLM spend, and DevAI's distillation needs are met by our own learn-stage summarizer. Adapter already exists. |
| **zep** | Keep, not default | Strong temporal knowledge-graph, but the current SDK path can't `forget()` per-record (compliance problem) and session-scoping maps awkwardly onto agent::repo. |
| **redis (legacy bridge)** | Fallback only | Fast, already wired, but keyword-only recall and manual index bookkeeping. Right answer when Postgres isn't available, wrong default. |
| **Vector DB SaaS (pinecone/qdrant/weaviate)** | Not now | Belongs in `adapters.vector_store` if ever needed; pgvector covers DevAI's scale comfortably. |

The architecture keeps this a config decision: `DEVAI_MEMORY_PROVIDER=mem0`
swaps the backend with no code change. "Best tool" is therefore: **pgvector
running by default, mem0 one env var away.**

Embedding model default: `text-embedding-3-small` (1536 dims — matches the
existing column) via the OpenAI LLM adapter's `embed()`. Configurable via
`DEVAI_EMBEDDING_PROVIDER` / `DEVAI_EMBEDDING_MODEL`.

---

## 3. Target architecture

```
                       ┌────────────────────────────────────────┐
   stages / chat /     │  MemoryAdapter (ABC, unchanged)        │
   agents / API        │                                        │
        │              │  InstrumentedMemoryAdapter (telemetry) │
        ▼              │     └─ wraps any concrete provider     │
   StageDeps.memory ──►│  pgvector │ redis │ mem0 │ zep │ hondo │ noop
                       └──────┬─────────────────────────────────┘
                              │ embedder (LLM family .embed())
                              ▼
                  Postgres agent_memories (vector + HNSW)
```

- **Adapters stay dumb storage.** Embedding, telemetry, scoping live in the
  factory-applied wrappers / injected embedder — call sites don't change.
- **Write path symmetry:** `sre_learn` (exists) + `alm_learn` (new) — every
  blueprint ends by distilling what future runs should know.
- **Read path:** `memory_injection` stays the front-loaded recall;
  `query_agent_memory` chat tool migrates to the adapter (Phase C).

---

## 4. Phases

### Phase A — make the existing machinery real  ✅ (this change)

| # | Change | Files |
|---|---|---|
| A1 | Embedding config block: `DEVAI_EMBEDDING_PROVIDER` (`auto\|openai\|none`), `DEVAI_EMBEDDING_MODEL`, `DEVAI_EMBEDDING_DIMENSIONS` | `src/devai/config.py` |
| A2 | `LLMEmbedder` — small wrapper turning the LLM adapter family's `embed(texts)` into the single-text `embed(text)` the pgvector adapter expects; built inside the memory factory (lazy, never raises) | `src/devai/adapters/memory/embedder.py`, `factory.py` |
| A3 | Filter pushdown: `Database.semantic_search(embedding, repo, agent, memory_type, limit)` — agent/type filters move into SQL; adapter stops post-filtering | `src/devai/services/database.py`, `pgvector_adapter.py` |
| A4 | `InstrumentedMemoryAdapter` — records `devai.memory.ops` counter + `devai.memory.duration_ms` histogram + hits per op via the global telemetry sink; applied to every provider in the factory (mirrors `InstrumentedLLMAdapter`) | `src/devai/adapters/memory/instrumented.py`, `factory.py` |
| A5 | `/readyz` surfaces `memory` check from `app.state.memory_adapter.health_check()` — reported but **non-fatal** (memory degrades, never blocks rollout) | `src/devai/webhook/app.py` |
| A6 | Local sandbox: `DEVAI_MEMORY_PROVIDER=redis` (kind sandbox always has Redis; pgvector needs the connected `Database` attached, which only the pipeline path guarantees) | `k8s/chart/values.yaml` |

### Phase B — close the learning loop  ✅ (this change, first slice)

| # | Change | Files |
|---|---|---|
| B1 | `alm_learn` stage: distills run outcome (stages completed/failed, review iterations, security verdict, error) into one episodic record + one procedural record when the run produced a fix pattern. Dry-run writes nothing. | `src/devai/pipeline/stages/lifecycle.py`, `src/devai/blueprint/registry.py` |
| B2 | Wire `alm_learn` into the ALM blueprint after the final stage | `blueprints/alm-pipeline.yaml` |
| B3 | (next) LLM-assisted distillation: when `deps.llm` is real, summarize "what should future runs on this repo know" into a semantic record instead of raw blobs | follow-up |
| B4 | (next) Chat session persistence: move `_conversations` to Redis (TTL) + end-of-session distill → memory | follow-up |

### Phase C — consolidate the dual system  (follow-up)

- Migrate `chat/agent.py`, `graph/orchestrator.py`, `agents/db_engineer.py`,
  `agents/tech_detector.py` off direct `AgentMemory` imports onto
  `MemoryAdapter` (via app.state / StageDeps).
- `AgentMemory` becomes a private implementation detail of
  `redis_adapter.py`; delete the duplicate read/write paths.
- Dashboard `/dashboard/api/memory` routes move to the adapter.

### Phase D — lifecycle + governance  (follow-up)

- Maintenance loop (reuse the SRE cron pattern): purge `expires_at` rows,
  apply relevance decay, merge near-duplicates (cosine > 0.95).
- Feedback scoring: bump `relevance_score` for memories present in
  successful runs.
- Dashboard memory panel: browse / search / edit / delete per agent/repo
  (cursor-parity plan `docs/plans/cursor-parity/05-memories.md` Phase 3).
- Schema additions (e.g. `embedding_model` column) go in
  `tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/devai/devai_db/`
  (+ the `devai-api/files/devai_db.sql` local mirror) — never new
  migrations in this repo.

---

## 5. Config surface (after Phase A)

```bash
DEVAI_MEMORY_PROVIDER=pgvector      # noop | redis | pgvector | mem0 | zep | hondo
DEVAI_EMBEDDING_PROVIDER=auto       # auto | openai | none
                                    #   auto: use openai when DEVAI_OPENAI_API_KEY is set, else none
DEVAI_EMBEDDING_MODEL=text-embedding-3-small
DEVAI_EMBEDDING_DIMENSIONS=1536     # must match agent_memories.embedding vector(1536)
```

Degradation chain is explicit and logged: no embedder → keyword recall;
no provider → noop; nothing ever crashes the pod.

## 6. Verification

- `pytest tests/unit/test_memory_adapters.py tests/unit/test_pipeline_stages*.py -v`
- `ruff check src/`
- Contract tests extended: embedder injection, instrumented delegate
  pass-through, SQL pushdown params, `alm_learn` behaviour (writes, dry-run,
  no-adapter degrade).
