# kagent Integration — long-lived agents over A2A

> **Status (2026-06-16): working end-to-end on prod.** A DevAI agent labelled
> `devai.io/runtime=kagent` is reconciled into a kagent-managed Deployment and is
> reachable over A2A; the dispatcher routes to it when the kagent switch is on.
> One **infra caveat** remains: kagent's model backend uses a placeholder OpenAI
> key — see [§6](#6-model-backend--reuse-the-existing-ai-gateway).

kagent (solo.io, `kagent-system`) is an opt-in runtime for **long-lived, standing**
agents — the complement to DevAI's default **ephemeral K8s Job per run**. Use kagent
for an agent that should stay resident and be addressable over A2A between runs; use
Jobs for per-run, fan-out pipeline work.

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
     systemPrompt: >-          # REQUIRED for kagent (see Bug 1)
       <the agent's system message>
   ```
2. **Re-seed the registry** — bump `reseedNonce` in
   `tesserix-k8s/charts/apps/devai-registry-bootstrap/values.yaml` (re-runs the
   bootstrap, which clones devai@main and POSTs the seeds), or `argocd app sync
   devai-registry-bootstrap`.
3. `kagent-agent-sync` (every 5 min) reconciles it into a kagent Deployment.
4. **Turn the switch on** — dashboard **Settings → kagent → on** (per user), or
   platform-wide via `DEVAI_KAGENT_ENABLED=true` in the `devai-api` chart.
5. Trigger a run that uses the agent → it routes over A2A; the api log shows
   `dispatched to kagent agent <name>`.

**Reference test target:** `document-analyzer-agent` is labelled `devai.io/runtime=kagent`.

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

DevAI-side enablement shipped earlier: routing + switch + default-OFF (devai `90fe2b9`,
`6acc10a`; k8s `9bbebc93`). Verified live: the rendered export is correct v1alpha2,
the Deployment is `Ready 1/1`, and the A2A endpoint round-trips (HTTP 200).

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

## 7. File map

| Concern | Where |
|---|---|
| DevAI dispatch + switch | `src/devai/pipeline/stages/job_runner.py`, `src/devai/agentic/kagent_client.py`, `src/devai/settings/models.py` (`kagent` connector), `src/devai/config.py` (`kagent_enabled`) |
| Registry export | `agentic-registry/adapters/kagent/kagent.go` |
| Reconcile CronJob | `tesserix-k8s/charts/apps/kagent-agent-sync/` |
| Controller + ModelConfig | upstream kagent chart (`tesserix-k8s/argocd/prod/infrastructure/kagent.yaml`), `tesserix-k8s/external-secrets/prod/kagent-system/` |
| Chart switch default | `tesserix-k8s/charts/apps/devai-api/` (`DEVAI_KAGENT_ENABLED`, `kagentUrl`) |
