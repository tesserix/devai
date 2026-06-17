# kagent Integration — long-lived agents over A2A

> **Status (2026-06-17): integration works end-to-end, but DORMANT on prod by
> design.** kagent agents are **always-warm standing pods** (one Deployment per
> labelled-agent × enabled-model), not per-call. On the current **3-node** prod
> cluster that doesn't pay off, so **no agent is labelled `devai.io/runtime=kagent`**
> today and **everything runs on-demand as Jobs** (see §0). The code + the dynamic
> Settings UI stay in place (default-off) for a future genuinely-hot agent once
> there's node headroom — see [§0a](#0a-re-enabling-kagent) to switch it back on.
>
> **Going further — Agent Substrate:** the path to re-enable kagent in a way that
> *fits* (Actors multiplexed in a gVisor WorkerPool, not a pod per agent) is the
> **Agent Substrate**. Full prod setup runbook + decision log + the hard-won
> gotchas: **[`SUBSTRATE-SETUP.md`](./SUBSTRATE-SETUP.md)**. Tracking: devai #69–#78.

---

## 0. Execution model — Jobs (default) vs kagent (opt-in)

DevAI runs every agent **on-demand** by default: `JobRunnerStage`
(`src/devai/pipeline/stages/job_runner.py`) submits **one ephemeral K8s Job per
agent run** — it spins up when the pipeline calls the agent, does the work, and
terminates. **Zero standing footprint.** kagent is the opposite: a resident
Deployment that's always running so it can be addressed over A2A with no
cold-start.

|                    | **Ephemeral Job** (default)            | **kagent** (opt-in)                         |
|--------------------|----------------------------------------|---------------------------------------------|
| Lifecycle          | pops up on call → runs → terminates     | always-running standing pod                 |
| Idle footprint     | **zero**                                | 1 pod per (labelled agent × enabled model)  |
| Cold start         | yes (Job scheduling, seconds)           | none (pod is warm)                          |
| Per-user LLM keys  | ✅ `PrincipalLLMResolver`               | ✅ ModelConfig `apiKeyPassthrough`          |
| Multi-model + fallback | ✅ `role_llm_*` / resolver          | ✅ per-model variants + dispatch chain      |
| Fits a 3-node cluster | ✅ always                            | only for a few hot agents with headroom     |

**Why kagent is off here.** Each kagent agent pod requests ~384Mi. Labelling all
40 registry agents × 4 enabled models = ~160 standing pods ≈ 60Gi — it doesn't fit
3 nodes, and most pipeline agents run briefly per-run (a perfect Job fit, a poor
standing-pod fit). Crucially, **per-user keys, multi-provider, and fallback already
work on the Job path** via `PrincipalLLMResolver` — so running on Jobs loses none
of that. kagent's *only* unique win is zero cold-start, which is worth a standing
pod only for an agent hit constantly (e.g. an interactive chat agent) **and** only
when the cluster has room.

**Rule of thumb:** default everything to Jobs. Reach for kagent for a specific,
constantly-hit agent where cold-start hurts — and label *just that one*.

### 0a. Re-enabling kagent

1. **Resolve the prompt at export time first.** 39 of 40 registry agents keep their
   system prompt in a referenced `Prompt` artifact (`spec.promptRef`), not inline.
   kagent requires a non-empty `systemMessage`, and the export does **not** resolve
   `promptRef` today — so labelling a promptRef-only agent renders an *invalid* CR
   (empty systemMessage, controller rejects it). Before labelling such an agent, add
   the one-line export resolution in `agentic-registry`: `kagent.Build` Options gain
   a `SystemPrompt`, set from a `resolveSystemPrompt` helper (follows `spec.promptRef`
   → `Prompt.spec.systemPrompt`), wired in both `v0ExportKagent*` handlers. (Agents
   with an inline `spec.systemPrompt`, like the old document-analyzer target, don't
   need this.)
2. **Label the one hot agent** (§2) — not all of them.
3. **Mind the pod budget** — pods = labelled agents × the models users enable in
   Settings. Keep both small on a 3-node cluster.

---

## 1. The end-to-end chain

```
AUTHORING            label agent  devai.io/runtime=kagent   (registry seed)
   │
REGISTRY (aregistry) GET /v0/export/kagent  → renders a kagent Agent CR
   │                 (adapters/kagent — emits kagent.dev/v1alpha2 spec.declarative)
RECONCILE            kagent-agent-sync CronJob (*/5)  → kubectl apply  → Agent CR
   │
CONTROLLER (kagent)  reconciles Agent → Deployment + A2A endpoint
   │                 {kagent_url}/api/a2a/{ns}/{agent}
DISPATCH (DevAI)     JobRunnerStage._maybe_dispatch_kagent → KagentClient (A2A)
                     …when the kagent switch is ON; else a K8s Job. Degrades to
                     Job on any kagent error (never a SPOF).
```

**Two halves:**

- **DevAI side** (this repo) — routing + the dynamic switch. Done & live.
  - `src/devai/pipeline/stages/job_runner.py::_maybe_dispatch_kagent` — routes a
    labelled agent over A2A; falls back to a Job on any error.
  - `src/devai/agentic/kagent_client.py` — A2A `message/send` client + `extract_a2a_text`.
  - `src/devai/registry/client.py` — surfaces `metadata.labels` on the Agent model.
  - The **switch**: a `kagent` Settings connector (`on`/`off` → `kagent_enabled`),
    resolved per run via the principal overlay (user→team→org→tenant→global→chart
    `DEVAI_KAGENT_ENABLED`). Default **OFF**. Takes effect next run, no restart.
- **Registry/runtime side** (`agentic-registry`, `tesserix-k8s`) — export + reconcile.

---

## 2. How to use it

1. **Label the agent seed** — `architecture/registry-seeds/agents/<name>.yaml`:
   ```yaml
   metadata:
     labels:
       devai.io/runtime: kagent
   spec:
     systemPrompt: >-          # REQUIRED unless the export resolves promptRef
       <the agent's system message>   # (see §0a) — kagent needs a non-empty
   ```                                #  systemMessage or the controller rejects it.
2. **Re-seed the registry** — bump `reseedNonce` in
   `tesserix-k8s/charts/apps/devai-registry-bootstrap/values.yaml` (re-runs the
   bootstrap, which clones devai@main and POSTs the seeds), or `argocd app sync
   devai-registry-bootstrap`.
3. `kagent-agent-sync` (every 5 min) reconciles it into a kagent Deployment.
4. **Turn the switch on** — dashboard **Settings → kagent → on** (per user), or
   platform-wide via `DEVAI_KAGENT_ENABLED=true` in the `devai-api` chart.
5. Trigger a run that uses the agent → it routes over A2A; the api log shows
   `dispatched to kagent agent <name>`.

**Current state:** **no agent is labelled** — `document-analyzer-agent` was the
reference target but was unlabelled (devai `7fa91f0`) when kagent went dormant, so
it runs on-demand as a Job like every other agent. Re-label it (or a hotter agent)
per §0a to bring kagent back.

---

## 3. Bugs found bringing this up, and their fixes

Each was only visible after fixing the prior one — recorded here so the next person
labelling an agent doesn't rediscover them.

| # | Symptom | Root cause | Fix | Commit |
|---|---|---|---|---|
| 1 | apply rejected: `spec.systemMessage should be at least 1 chars` | DevAI seeds use `skill`+`promptRef`; the export builds `systemMessage` from `systemPrompt`/`description`, which seeds lacked | add `systemPrompt` to the seed | devai `e6190dc` |
| 2 | controller **nil-panic**, no Deployment | export emitted the **pre-0.9 flat** Agent spec (`modelConfig`/`systemMessage` on `spec`); v0.9 prunes them → `spec.declarative` nil | nest under `spec.declarative` + `spec.type: Declarative` | aregistry `ffef827` |
| 3 | apply rejected: `.spec.declarative: field not declared in schema` | export stamped `apiVersion: kagent.dev/v1alpha1`, whose schema has no `declarative` (v1alpha2 is the storage version) | emit the Agent as **`kagent.dev/v1alpha2`** | aregistry `c9403a1` |
| 4 | apply rejected: `.spec.type: field not declared in schema` | `kubectl apply --server-side` mis-negotiates the schema for a **multi-version** CRD (v1alpha1 served + v1alpha2 storage) | **client-side apply** in the agent-sync CronJob | k8s `c86c4a6e` |
| 5 | agent-sync apply **OOMKilled** (exit 137), never re-renders | client-side apply parses the cluster OpenAPI schema → spikes past the 128Mi limit | **256Mi + `--validate=false`** (skip the OpenAPI parse) | k8s `9640e533` |
| 6 | LLM call `404 not_found_error: model claude-sonnet-4-20250514` | kagent calls the provider **directly** (no DevAI gateway model-mapping); that id 404s on Anthropic's API | use a **direct-valid** model id (`claude-sonnet-4-5-20250929`) | k8s `c66fbb5b` |

DevAI-side enablement shipped earlier: routing + switch + default-OFF (devai `90fe2b9`,
`6acc10a`; k8s `9bbebc93`). **Verified end-to-end (2026-06-16):** `document-analyzer-agent`
on the passthrough ModelConfig, A2A dispatch with a real Anthropic key as Bearer →
`status: completed` (no 401, no 404) — per-user passthrough works.

**Mitigations against recurrence:** (a) kagent uses **direct provider model ids**, not
DevAI's gateway aliases — keep the kagent ModelConfig model in sync with what the
provider's API actually accepts (the `/settings/models/{provider}` endpoint lists
valid ids per key); (b) the agent-sync always uses **client-side `--validate=false`
apply** with 256Mi headroom for the multi-version CRDs; (c) the export pins
**v1alpha2** for Agents.

---

## 4. Verifying on the cluster

```bash
# reconciled agent + Deployment
kubectl -n kagent-system get agents.kagent.dev document-analyzer-agent    # READY/ACCEPTED True
kubectl -n kagent-system get deploy document-analyzer-agent               # 1/1
# the A2A endpoint DevAI dispatches to (port-forward + JSON-RPC message/send)
kubectl -n kagent-system port-forward deploy/kagent-controller 18083:8083 &
curl -s -X POST localhost:18083/api/a2a/kagent-system/document-analyzer-agent \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"ping"}],"messageId":"m1"}}}'
```

---

## 5. Operational notes

- **Labelling an agent stands up a real kagent Deployment** (the reconciler acts
  regardless of the DevAI switch). The switch only gates DevAI's *dispatch* decision.
- **Multi-version CRD:** always **client-side** `kubectl apply` for kagent Agents
  (the agent-sync now does). Server-side apply breaks on the v1alpha1/v1alpha2 split.
- **Re-seeding:** the bootstrap re-runs only when its chart changes (it lives in
  `tesserix-k8s`, the seed lives in devai@main) — bump `reseedNonce` to force it.
- **Tools — TODO:** the export still emits the old `ToolServer` ref shape; kagent
  v0.9 uses `RemoteMCPServer` + `toolNames`. Not exercised (no tool-bearing labelled
  agent yet). Port `adapters/kagent/kagent.go` before labelling an agent with
  `mcp_servers`.

---

## 6. Model backend — does kagent need its own LLM keys?

**Short answer: no. DevAI's existing LLM plane is enough — kagent should SHARE it,
and needs no new Anthropic/Vertex/Bedrock/OpenAI wiring.** But there's an important
architectural caveat about *which* key it shares (platform, not per-user).

### 6a. What's in GCP SM today (verified 2026-06-16)

| Secret | Layer | Real? |
|---|---|---|
| `prod-devai-anthropic-api-key` | platform | **REAL** (`sk-ant…`) |
| `prod-devai-vertex-api-key` | platform | **REAL** (`AQ.A…`) |
| `prod-devai-openai-api-key` | platform | **PLACEHOLDER** ← kagent's `kagent-openai` syncs from this |
| `prod-devai-groq-api-key` | platform | PLACEHOLDER |
| `devai-user-<uid>-llm-default-anthropic_api_key` | per-user | **REAL** |
| `devai-user-<uid>-llm-default-openai_api_key` | per-user | **REAL** |

So kagent's 401 is simply that it points at the **one placeholder** platform key
(OpenAI). DevAI's real platform providers are **Anthropic and Vertex**. No Bedrock
exists or is used anywhere in DevAI — don't add it for kagent.

### 6b. Two ways to feed keys to kagent

kagent supports **all the providers DevAI uses** — `Anthropic, OpenAI, AzureOpenAI,
Gemini, GeminiVertexAI, AnthropicVertexAI, Bedrock, Ollama`. There are two key modes,
and the right answer depends on whether you want per-user keys.

**Mode 1 — shared platform key (`apiKeySecret`).** The ModelConfig holds a static
secret ref; every call uses one shared key. Simple; fine for platform agents that
don't need per-user billing. Today this is OpenAI + the placeholder → 401.

**Mode 2 — per-user keys via `apiKeyPassthrough` (the answer to "each user uses their
own key").** kagent ModelConfig has a boolean **`apiKeyPassthrough`**:

> *"forwards the Bearer token from incoming A2A requests directly to the LLM provider
> as the API key … for federated identity, to avoid separate secret management."*
> (mutually exclusive with `apiKeySecret`.)

This is purpose-built for exactly this. **One shared kagent agent Deployment** serves
every user, and each A2A request carries *that user's* key as the `Authorization:
Bearer` token, which kagent forwards to the provider. **No per-user Deployments, no
syncing per-user secrets into `kagent-system`.** It reuses DevAI's existing per-user
resolution — the same `PrincipalLLMResolver` overlay that already gives the **Job**
path the triggering user's key.

### 6c. Recommended design — per-user kagent via passthrough

```
DevAI dispatch (JobRunnerStage._maybe_dispatch_kagent)
  1. resolve the principal's LLM connector (provider, model, KEY) via the overlay
     — already done for the Job path (settings_overlay / PrincipalLLMResolver)
  2. KagentClient.dispatch sends Authorization: Bearer <user's LLM key>
     (today it only sends X-Forwarded-User + the bff service token)
        │  POST {kagent}/api/a2a/kagent-system/<agent>
        ▼
kagent agent  (ModelConfig: apiKeyPassthrough=true, provider=Anthropic, claude-sonnet-4)
  3. forwards the Bearer token to the provider as the API key → user's own key, billed to them
```

**What to build (two small, well-scoped changes):**
- **Infra (`tesserix-k8s`):** a ModelConfig with `apiKeyPassthrough: true` +
  `provider: Anthropic` + `model: claude-sonnet-4-20250514`; point
  `kagent-agent-sync` `registry.modelConfig` at it. No secret needed (passthrough).
- **DevAI (`src/devai/agentic/kagent_client.py` + `job_runner.py`):** resolve the
  principal's key from their connector (the overlay already exposes it) and pass it as
  `Authorization: Bearer …` on `dispatch()`. Falls back to the shared ModelConfig (or
  the Job path) when the user has no own key.

**Provider note:** one ModelConfig fixes the *provider* (the key varies per user). DevAI
standardizes on Anthropic/Claude, so an Anthropic passthrough ModelConfig fits most
agents. For users on a different provider, dispatch to a provider-matched passthrough
agent variant, or fall back to the Job path. Vertex/Bedrock are available the same way
if needed.

**Simple baseline (if you just want kagent working now, before per-user):** point
`default-model-config` at the **real Anthropic platform key** — add a `kagent-anthropic`
ExternalSecret (sync `prod-devai-anthropic-api-key`) and set the ModelConfig to
`provider: Anthropic`. Shared key, but real, so agents run. Layer passthrough on top
afterward for per-user.

> **Status:** design verified against the live CRD (`apiKeyPassthrough` exists,
> all providers supported). Not yet implemented — it's a `tesserix-k8s` ModelConfig +
> a small `kagent_client` change. Correcting the earlier note in this doc's history that
> said kagent "cannot use per-user keys" — with `apiKeyPassthrough`, it can.

---

## 7. Isolation & guardrails (passthrough)

Passthrough forwards a real LLM credential, so isolation matters. The model:

**The core property — you can only ever use a key you already hold.** DevAI
resolves the key from the *triggering principal's own* Settings connector overlay
(per-principal), so user A's run can only carry user A's key. There is no path by
which A's run forwards B's key.

**DevAI-side guardrails (enforced in `job_runner.py::_maybe_dispatch_kagent`):**

| Guardrail | Effect |
|---|---|
| **Human-principal only** | system/webhook/cron runs never forward a key (they'd resolve a service/global key) → they fall back to the Job path |
| **Connector-scoped only** | `_kagent_user_key` returns a key only when it came from the principal's connector overlay (`overlaid_attrs`) — **never the platform base key** (`DEVAI_ANTHROPIC_API_KEY`), which is not an overlay override |
| **Per-principal** | the overlay is built from the run's authenticated `Principal`; no user receives another user's personal key (their connector is keyed to their uid/email) |
| **Provider-matched** | only the key for `kagent_model_provider` is forwarded — a user on a different provider → no key → Job |
| **No own key → Job** | a kagent run is never billed to a shared/platform key under passthrough |
| **Never logged / persisted** | the key rides only the `Authorization` header; it is not logged and not written to task state or the A2A result |

**Trust assumptions / defense-in-depth (infra — recommended):**

1. **The key transits the kagent controller** (DevAI → `/api/a2a/…` → agent → LLM).
   So the kagent control plane sees the Bearer. Mesh traffic is mTLS-encrypted
   (Istio); **verify the kagent controller/agent does not log `Authorization`
   headers** (upstream solo.io component — a trust assumption of passthrough).
2. **Restrict who can call the A2A endpoint.** The internal `:8083` A2A endpoint is
   unauthenticated cluster-internal and `allow-mesh-internal` is permissive — any
   meshed pod can reach it. This does **not** let anyone use another user's key (key
   possession still governs), but to stop resource abuse / identity-header spoofing,
   add an Istio `AuthorizationPolicy` in `kagent-system` allowing `/api/a2a/*` only
   from the **devai-api ServiceAccount**, plus a matching `NetworkPolicy`. (Not yet
   implemented — recommended next infra step; pattern in
   `tesserix-k8s/manifests/agentic-istio/authorization-policy.yaml`.)
3. **Identity forwarding stays gated** by `X-Auth-Bff-Secret` (the receiver drops a
   spoofed `X-Forwarded-User` without it) — unchanged by passthrough.

## 8. Multi-provider, multi-model & fallback (roadmap)

**Goal:** a user picks their provider(s) + model(s) in Settings (OpenAI, Vertex,
Anthropic, …) and kagent uses them, with fallback — just like the Job path.

**What already exists (reuse, don't rebuild):**
- The LLM connector carries `provider`, per-provider model fields, `enabled_models`
  (a list), and `fallback_model` (`settings/models.py`, `settings/overlay.py`).
- `GET /settings/models/{provider}` lists the models a user can use **evaluated
  against their own key** (`adapter.list_models()`), with `KNOWN_PROVIDERS` =
  anthropic, openai, vertex_gemini, groq, openrouter, gateway, noop.
- The **Job path already does** per-user multi-provider + fallback via the role chain
  (`PrincipalLLMResolver` / `role_llm_or`).

**The kagent constraint that shapes the design:** a kagent Agent references ONE static
`ModelConfig` (provider + model fixed at reconcile). There is **no per-request
provider/model** — only the *key* varies per request (passthrough). So per-user
provider/model means **pre-provisioned ModelConfig variants**, not free-form.

**Design — a bounded ModelConfig catalog + provider/model-aware dispatch + fallback:**

```
Settings (per user)            kagent (pre-provisioned, passthrough)        DevAI dispatch
─────────────────────          ──────────────────────────────────          ──────────────
provider: openai               kagent-anthropic-sonnet-4-5  ┐               1. read user's provider+model
model: gpt-4.1                 kagent-openai-gpt-4-1        ├ one Agent      +fallback from the overlay
enabled_models: [...]          kagent-vertex-gemini-2-5     ┘ variant each   2. pick the matching variant
fallback_model: claude-…                                                    3. forward user key (Bearer)
                                                                            4. on failure → next (fallback)
```

- The agent-sync renders **one agent variant per catalog ModelConfig** (e.g.
  `document-analyzer-agent`, `…-openai`, `…-vertex`), each an `apiKeyPassthrough`
  ModelConfig for that provider+model. The catalog is **bounded** — the platform's
  offered models, not per-user — so no explosion.
- DevAI's `_maybe_dispatch_kagent` resolves the user's `(provider, model)` + fallback
  from the overlay, dispatches to the matching variant with the user's key, and on a
  provider/model error **re-dispatches to the fallback** variant.
- **Settings UI:** surface which providers/models kagent supports (the catalog ∩
  `/settings/models/{provider}` for the user) so users only enable what they have a
  key for.

**Phasing:** Phase 1 (done) — single Anthropic passthrough, per-user key. Phase 2 —
per-**provider** catalog (anthropic/openai/vertex) + provider-aware dispatch + fallback
across providers (bounded). Phase 3 — per-(provider, model) granularity honoring
`enabled_models`/`fallback_model`, + the Settings UI catalog. Full *arbitrary* per-user
model is impractical (ModelConfig/agent explosion) — the bounded catalog is the sweet
spot.

## 9. File map

| Concern | Where |
|---|---|
| DevAI dispatch + switch | `src/devai/pipeline/stages/job_runner.py`, `src/devai/agentic/kagent_client.py`, `src/devai/settings/models.py` (`kagent` connector), `src/devai/config.py` (`kagent_enabled`) |
| Registry export | `agentic-registry/adapters/kagent/kagent.go` |
| Reconcile CronJob | `tesserix-k8s/charts/apps/kagent-agent-sync/` |
| Controller + ModelConfig | upstream kagent chart (`tesserix-k8s/argocd/prod/infrastructure/kagent.yaml`), `tesserix-k8s/external-secrets/prod/kagent-system/` |
| Chart switch default | `tesserix-k8s/charts/apps/devai-api/` (`DEVAI_KAGENT_ENABLED`, `kagentUrl`) |
