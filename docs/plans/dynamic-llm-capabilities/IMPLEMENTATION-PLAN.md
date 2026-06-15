# Implementation Plan — Dynamic, capability-aware LLM routing

**Status:** Phase 1–4 shipped 2026-06-15
**Owner:** platform / LLM adapters
**Goal:** DevAI should *know which LLM providers are actually connected* (Anthropic,
OpenAI/Codex, Vertex, Groq, …), *discover the right model for each agent on those
providers*, and *continue without failures* — instead of pinning one vendor's model
ids and hoping the resolved provider can serve them.

---

## Problem

Today each agent/role pins a concrete model id (`claude-opus-4-8`, `o3`, …) via
`llm_model_<role>` / spec `llm_model`. Two gaps:

1. **Static, not connection-aware.** The router doesn't *ask* "which providers does
   this tenant actually have keys for?" It pins a model and only *reacts* when the
   call fails. We already added two safety nets — the `model_policy` guard (a
   mismatched id falls back to the provider's **generic default**) and the ordered
   fallback chain (`anthropic → openai → vertex → groq`). Those stop *failures*, but
   a heavy coding agent forced onto OpenAI gets `gpt-4.1`-default, not `o3` — the
   *wrong* model for the job.

2. **No introspection.** Nothing reports "you're connected to Anthropic + Groq, so
   role X resolves to model Y." Operators can't see how a run will be routed, and a
   run can't pre-flight "do I have *any* usable provider?".

## Outcome

Given the set of **connected** providers for a principal (their own connectors, or
the platform), the system:

- picks, **per role/agent**, the *tier-appropriate* model on the **first connected
  provider** (Anthropic → OpenAI → Vertex → Groq preference), and chains the rest as
  fallbacks each on **their own** tier model;
- exposes **what's connected** and **how each role resolves** (introspection +
  API);
- **pre-flights** a run so "no provider connected" fails fast with a clear
  "add a key in Settings" message instead of mid-pipeline.

The configured `llm_model_<role>` stays the source of truth for the *tier* and for
the exact id **when its own provider is connected** — so a fully Anthropic tenant is
byte-for-byte unchanged.

---

## Design

### Capability map (the one place that knows "provider × tier → model")

`src/devai/adapters/llm/capabilities.py`:

```
PROVIDER_TIER_MODELS = {
  "anthropic":     {light: claude-haiku-4-5, standard: claude-sonnet-4-6, heavy/frontier: claude-opus-4-8},
  "openai":        {light: gpt-4.1-mini,     standard: gpt-4.1,           heavy/frontier: o3},
  "vertex_gemini": {light: gemini-2.5-flash, standard/heavy/frontier: gemini-2.5-pro},
  "groq":          {*: llama-3.3-70b-versatile},
  "gateway":       {= anthropic ids (it routes claude-on-vertex aliases)},
  # openrouter omitted → uses its configured default model
}
```

Tiers are the existing `light | standard | heavy | frontier`. Adding a provider or
re-pointing a tier is a one-line edit here — no spec/role changes.

### Functions

- `tier_for_model(model) -> tier` — infer the tier of a configured role model
  (`opus`/`o3`→heavy, `haiku`/`mini`/`nano`/`flash-lite`→light, else standard). Makes
  the configured `llm_model_<role>` the single source of tier truth — **no role
  table to maintain**.
- `natural_provider(model) -> provider|""` — `claude→anthropic`, `gpt/o*→openai`,
  `gemini→vertex_gemini`, `llama/mixtral→groq`.
- `model_for(provider, tier) -> model|""` — `PROVIDER_TIER_MODELS[provider][tier]`,
  `""` when unknown (→ provider default).
- `ordered_providers(settings, prefer=None) -> [provider]` — preference order:
  `prefer` (the role model's natural provider) → `llm_provider` → gateway
  (`llm_role_chain_provider`) → `llm_fallback_provider` order; deduped, registry-known.
- `connected_providers(settings, prefer=None) -> [provider]` — `ordered_providers`
  filtered to those that build a **non-noop** adapter (i.e. have creds). This is the
  dynamic "what's actually connected".
- `describe_capabilities(settings) -> {connected, primary, roles:{role:{provider,model}}}`
  — introspection the system (and the API) uses to *report* its config.

### Wiring — `create_role_llm` becomes capability-aware (Phase 2)

```
model = normalize_model(llm_model_<role>)          # de-fabled
if not model:                                      # role has no opinion
    return ordered fallback chain (each link its own default)   # = today's role="" path
tier   = tier_for_model(model)
order  = ordered_providers(settings, prefer=natural_provider(model))
links  = []
for i, p in connected(order):
    pm = model if (i == 0 and provider_serves(p, model)) else model_for(p, tier)
    links.append(PinnedModel(adapter(p), pm))      # each link on ITS tier model
chain  = FallbackLLMAdapter(*links, preserve_model=False)   # fallbacks use their own pin
```

Result for a **heavy** role (`dev_api`, model `claude-opus-4-8`):

| Connected providers          | Resolved chain                                   |
|------------------------------|--------------------------------------------------|
| Anthropic (+ gateway, …)     | `claude-opus-4-8` → gateway → openai `o3` → …     |
| OpenAI + Groq (no Anthropic) | openai **`o3`** → groq `llama-3.3-70b`           |
| Groq only                    | groq `llama-3.3-70b`                              |
| none                         | default adapter (noop-degrades, clear message)   |

The configured id is honored on its own provider (Anthropic tenant unchanged); every
*other* connected provider contributes its **tier-appropriate** model, not a generic
default. `model_policy` remains the final safety net.

### Spec agents

`_select_llm` already builds `spec-provider → platform chain`; the platform chain is
now capability/ordered. Optionally (Phase 2b) pin the platform fallback to the spec's
tier — deferred; current behavior already degrades correctly.

---

## Phases

- **Phase 1 — capabilities module** ✅ `capabilities.py` + unit tests
  (`tier_for_model`, `natural_provider`, `model_for`, `ordered/connected_providers`,
  `describe_capabilities`).
- **Phase 2 — capability-aware `create_role_llm`** ✅ build the per-link tier-pinned
  chain from connected providers; cache key already includes provider creds +
  `llm_fallback_provider`. Tests assert the resolved chain per connected set.
- **Phase 3 — introspection surface** ✅ `GET /api/settings/llm/capabilities`
  (per-principal overlay → `describe_capabilities`: connected providers + per-role
  resolution; read-only, no keys) + dashboard `LlmCapabilitiesPanel` on Settings
  ("Connected: … → each agent's provider · model", refetched after each save).
- **Phase 4 — run preflight** ✅ `_llm_preflight` in `pipeline/routes.py`, called by
  `POST /api/pipeline/runs` and `/api/pipeline/trigger`: blocks (400, clear "connect
  a provider" message) only when ZERO providers are connected for the caller AND no
  trial budget. Conservative — skips without `app.state.config`, never blocks on its
  own resolution error, never fires when any key is configured.

## Non-goals

- Live model **discovery** via `list_models()` (provider catalogs) — the static
  capability map is deterministic and testable; revisit only if vendors churn ids.
- Per-tenant tier overrides beyond the existing `llm_tier_*` envs.

## Test plan

- `tests/unit/test_llm_capabilities.py` — map/tier/provider helpers; `describe_*`.
- `tests/unit/test_llm_adapters.py` — `create_role_llm` resolves the right chain for
  {anthropic-only, openai+groq, groq-only, none}; Anthropic tenant byte-for-byte
  unchanged; ordering preserved.

## Rollback

Pure adapter-layer change behind `create_role_llm`. Revert the two files; the
`model_policy` guard + ordered fallback (already shipped) keep "no failures".
