# 01 — Codebase Indexing & Semantic Retrieval

**Cursor parity:** `@codebase` / project‑aware retrieval. **Priority: P0.**

## What Cursor does

Builds a continuously‑synced semantic index of a repo so any agent can retrieve
the *relevant* code for a task instead of stuffing whole files into context.

## How it works (the concepts to steal)

1. **Merkle‑tree change detection.** Hash every file; roll hashes up through
   directories to a root. On change, only the edited file + its ancestor hashes
   change. Diffing the client tree against the server tree finds exactly which
   chunks need re‑embedding — O(changes), not O(repo). Periodic sync (~5 min).
2. **AST‑aware chunking.** Split with `tree-sitter`, walking the syntax tree
   depth‑first so chunks respect function/class boundaries (not blind N‑line
   windows). Better chunks → better retrieval.
3. **Per‑chunk embeddings in a vector DB.** Embed each chunk; store vectors +
   metadata (path, symbol, line range, language). Cursor uses Turbopuffer; we use
   **pgvector** (already in our stack).
4. **Cache by chunk hash.** Unchanged chunk hash → reuse embedding, skip the
   model call. Index stays cheap to maintain.
5. **Privacy posture.** Cursor stores only vectors + obfuscated paths. We're
   single‑tenant in‑cluster, so we can store plaintext, but keep the metadata
   layer pluggable for a future hosted mode.

## DevAI mapping (framework)

- New adapter family **`adapters/vector_store/`** (ABC + `pgvector`, `qdrant`,
  `noop`). This is already a *planned* family in `CLAUDE.md`.
- New subsystem **`src/devai/indexing/`**: `merkle.py` (tree + diff), `chunker.py`
  (tree‑sitter), `indexer.py` (orchestrates embed + upsert), `retriever.py`
  (`semantic_search(query, repo, k, filters)`).
- Embeddings via the existing **`adapters/llm`** family (add an `embed()` surface)
  or a dedicated `adapters/embeddings`. Reuse the Anthropic/OpenAI/Groq creds.
- Wire into the SCM layer: `scm/` already clones repos for onboarding — index on
  onboard, re‑index on push webhook (delta only).
- Expose as a **tool** (`tools/codebase_search.py`) so every LangGraph agent and
  the chat agent can call it, and as `GET /api/index/search`.

## Implementation plan

- **Phase 1 — store.** `adapters/vector_store` ABC + pgvector backend. Schema
  (`code_chunks(repo, path, symbol, start_line, end_line, lang, content_hash,
  embedding vector(1536), updated_at)`) added in **tesserix‑k8s** db‑schema‑bootstrap,
  not here. Contract test.
- **Phase 2 — chunk + embed.** `chunker.py` (tree‑sitter grammars for py/ts/go),
  `indexer.py` full‑repo build with hash cache.
- **Phase 3 — incremental sync.** `merkle.py`; on push webhook compute changed
  paths → re‑chunk/re‑embed only those; delete vectors for removed files.
- **Phase 4 — retrieval surface.** `retriever.py`, `tools/codebase_search.py`,
  REST endpoint, inject into agent context (top‑k by task).
- **Phase 5 — eval.** Retrieval hit‑rate harness; tune chunk size + k.

## Files & modules

```
src/devai/adapters/vector_store/{base,factory,pgvector,qdrant,noop}.py
src/devai/indexing/{merkle,chunker,indexer,retriever}.py
src/devai/tools/codebase_search.py
tests/unit/test_vector_store_adapters.py
tests/unit/test_indexing.py
```

## Config (`DEVAI_*`)

```
DEVAI_VECTOR_STORE_PROVIDER=pgvector        # pgvector|qdrant|noop
DEVAI_EMBEDDING_PROVIDER=openai             # reuse llm creds
DEVAI_EMBEDDING_MODEL=text-embedding-3-small
DEVAI_INDEX_CHUNK_MAX_TOKENS=400
DEVAI_INDEX_SYNC_ON_PUSH=true
```

## Acceptance criteria

- Onboarding a repo populates `code_chunks`; a push re‑embeds only changed files
  (verified by embedding‑call count ≪ file count).
- `semantic_search("where is auth handled", repo)` returns correct files top‑3.
- Backend swaps pgvector→qdrant via one env var, contract tests green.
- Vector store down → retriever degrades to Noop, pipeline still runs.

## Sources

- [Securely indexing large codebases · Cursor](https://cursor.com/blog/secure-codebase-indexing)
- [How Cursor Indexes Codebases Fast · Engineer's Codex](https://read.engineerscodex.com/p/how-cursor-indexes-codebases-fast)
- [How Cursor Actually Indexes Your Codebase · Towards Data Science](https://towardsdatascience.com/how-cursor-actually-indexes-your-codebase/)
