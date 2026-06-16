# kagent — per-user multi-provider / multi-model / fallback

**Status:** Planned — 2026-06-16. Builds on the working per-user Anthropic passthrough.
**Goal:** a user picks provider(s) + model(s) in Settings (OpenAI, Vertex, Anthropic, …);
kagent runs the agent on the user's chosen model with their own key, and falls back to
the next model/provider on failure — parity with the Job path.

---

## What already exists (reuse, do not rebuild)

- **LLM connector** carries `provider`, per-provider model fields, **`enabled_models`**
  (list) and **`fallback_model`** — `settings/models.py`, surfaced in the overlay as
  `llm_enabled_models` / `llm_user_fallback_model` (`settings/overlay.py`).
- **`GET /settings/models/{provider}`** lists models a user can use **evaluated against
  their own key** (`adapter.list_models()`); `KNOWN_PROVIDERS` = anthropic, openai,
  vertex_gemini, groq, openrouter, gateway, noop.
- **Job path** already resolves per-user provider+model+fallback (`PrincipalLLMResolver`,
  `role_llm_or`).
- **Passthrough** is proven: DevAI forwards the user's key as the A2A Bearer; the kagent
  ModelConfig is `apiKeyPassthrough` (no shared key).

## The constraint that shapes everything

A kagent **Agent references ONE static `ModelConfig`** (provider + model fixed at
reconcile); there is no per-request provider/model. So **each (agent, provider, model)
is a distinct Agent CR → a distinct Deployment (pod).** A full cross-product catalog
would be `agents × providers × models` standing pods — too many. The mitigation is
**lazy provisioning**: only stand up the variant a user actually selects, and reap idle
ones.

---

## Design

### Naming
- ModelConfig: `kagent-mc-<provider>-<model-slug>` (e.g. `kagent-mc-anthropic-sonnet-4-5`).
  `apiKeyPassthrough: true`, `provider`, **direct-provider-valid** `model` id.
- Agent variant: `<agent>--<provider>-<model-slug>` (DNS-1123). Same prompt/tools as the
  base agent, referencing the matching ModelConfig.

### Curated catalog (bounded), not "every model"
A `kagentModels` list in the kagent-agent-sync chart enumerates the **offered**
(provider, model) pairs — a small curated set per provider (a default + maybe a premium),
using **direct-provider-valid** ids (validated against `/settings/models/{provider}`),
e.g.:
- anthropic: `claude-sonnet-4-5-20250929`
- openai: `gpt-4.1`, `o3`
- vertex (GeminiVertexAI): `gemini-2.5-pro`

The **ModelConfigs** for the whole catalog are cheap (CRs, no pods) and chart-provisioned.
The **Agent variants** (pods) are **lazy** (below).

### DevAI dispatch (`_maybe_dispatch_kagent`)
1. Resolve the principal's overlay → `(provider, model)` (their connector + chosen model),
   plus the **fallback chain** from `enabled_models` / `fallback_model`.
2. Intersect with the curated catalog → an ordered list of `(provider, model)` to try.
3. For each, ensure the variant exists (lazy provision; see below), dispatch to
   `<agent>--<provider>-<model-slug>` with the user's key as Bearer.
4. On a provider/model error (4xx model/auth, 429), **re-dispatch to the next** in the
   chain. Exhausted → Job path.

### Lazy provisioning + reaping (avoids the pod explosion)
- DevAI provisions an Agent variant **on first dispatch** that needs it (via the registry
  → agent-sync, or a thin DevAI K8s call gated by RBAC), annotated with `last-used`.
- A reaper (extend `kagent-agent-sync`) **deletes variants idle > TTL** (e.g. 24h). The
  ModelConfigs stay (cheap); only pods are transient.
- Net: standing pods ≈ the variants actually in active use, not the full cross-product.

### Settings UI
- A `kagent` section shows the **catalog ∩ what the user has a key for** (reuse
  `/settings/models/{provider}`), so users enable only usable (provider, model)s and set
  a fallback. Drives the dispatch chain above.

---

## Phases (each ships independently)

**A — provider-level (bounded, no lazy needed yet).** Chart-provision one passthrough
ModelConfig per *provider* (default model) + render one Agent variant per provider for
each labelled agent (agent-sync). DevAI dispatch picks the variant by the user's
**provider**, forwards the key, falls back across providers. Pods = agents × providers
(small). Covers most users.

**B — model-level + lazy.** Extend the catalog to (provider, model); add lazy provisioning
+ idle reaper; dispatch honors `enabled_models` order + `fallback_model`.

**C — Settings UI.** Surface the kagent catalog (∩ user keys) + enable/fallback controls
in the dashboard Settings page.

## File touch-points
- `tesserix-k8s/charts/apps/kagent-agent-sync/`: ModelConfig catalog (`templates/modelconfig.yaml`
  → loop over `values.kagentModels`), variant rendering, idle reaper.
- `agentic-registry/adapters/kagent` + `internal/api/export.go`: render per-(provider,model)
  variants (or DevAI provisions them).
- `devai/src/devai/pipeline/stages/job_runner.py`: provider/model resolution + fallback
  re-dispatch; `devai/src/devai/config.py`: catalog awareness.
- `devai/dashboard/src/app/settings/`: kagent catalog UI.

## Mitigations carried in
- **Direct-provider-valid model ids only** (not gateway aliases) — validate against
  `/settings/models/{provider}`.
- **Bounded curated catalog + lazy variant pods + idle reaping** — no cross-product explosion.
- **Isolation guardrails unchanged** — per-variant dispatch still forwards only the
  authenticated human principal's connector-scoped key.
