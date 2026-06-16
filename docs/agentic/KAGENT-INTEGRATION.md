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

### 6b. The architectural point: kagent shares the PLATFORM key, not per-user keys

- **DevAI's per-user keys** (`devai-user-<uid>-…`) are resolved **per run** by the
  `PrincipalLLMResolver` overlay and injected into each ephemeral **Job** — so a run
  bills against *the triggering user's own* key (per-user isolation + metering).
- **A kagent agent is a long-lived, shared Deployment** with a **static `ModelConfig`**
  fixed at reconcile time. It has no per-A2A-request model context, so it **cannot use
  per-user keys** — every call rides one **shared platform key**, regardless of who
  triggered it (the A2A protocol forwards identity, not model credentials).
- **Implication:** routing an agent to kagent means its LLM calls use the **shared
  platform key** and lose per-user attribution/metering. If per-user billing matters
  for that agent, keep it on the **Job path** (switch off / don't label it). This is a
  deliberate trade-off of the standing-runtime model — not a bug. True per-user LLM in
  kagent would require passing the user's model config through every A2A request *and*
  a per-request dynamic ModelConfig, which kagent does not support.

### 6c. Recommendation (reuse, don't rewire)

kagent should **not** own a separate key plane. Pick one, best first:

1. **Switch kagent's `default-model-config` to Anthropic** (DevAI's real platform key).
   Add an ExternalSecret `kagent-anthropic` (sync `prod-devai-anthropic-api-key` into
   `kagent-system`, mirroring `kagent-openai`) and set the ModelConfig to
   `provider: Anthropic`, `model: claude-sonnet-4-20250514` (DevAI's `DEVAI_CLAUDE_MODEL`).
   One real key, already owned by DevAI, already in the cluster. **Most reliable.**
2. **Route through `devai-ai-gateway`** (single key plane): set the ModelConfig
   `baseURL` to the gateway's OpenAI-compatible route
   (`http://ai-gateway.agentgateway-system.svc.cluster.local:8080/openai`) so the
   gateway injects the key — but only after the gateway's OpenAI upstream has a real
   key (today the platform OpenAI key is the placeholder).
3. **Populate the real OpenAI key** in `prod-devai-openai-api-key` (the ExternalSecret
   re-syncs). Works, but keeps OpenAI as a second key plane DevAI otherwise barely uses.

> **Action (operator-owned infra change in `tesserix-k8s` + GCP SM, not done here):**
> implement option 1 — point kagent at the real Anthropic platform key. Touches the
> kagent chart's ModelConfig + a new `kagent-anthropic` ExternalSecret; `default-model-config`
> is created by the upstream chart, so override it via chart values or replace it with an
> Anthropic ModelConfig and update `registry.modelConfig` in
> `tesserix-k8s/charts/apps/kagent-agent-sync/values.yaml`.

---

## 7. File map

| Concern | Where |
|---|---|
| DevAI dispatch + switch | `src/devai/pipeline/stages/job_runner.py`, `src/devai/agentic/kagent_client.py`, `src/devai/settings/models.py` (`kagent` connector), `src/devai/config.py` (`kagent_enabled`) |
| Registry export | `agentic-registry/adapters/kagent/kagent.go` |
| Reconcile CronJob | `tesserix-k8s/charts/apps/kagent-agent-sync/` |
| Controller + ModelConfig | upstream kagent chart (`tesserix-k8s/argocd/prod/infrastructure/kagent.yaml`), `tesserix-k8s/external-secrets/prod/kagent-system/` |
| Chart switch default | `tesserix-k8s/charts/apps/devai-api/` (`DEVAI_KAGENT_ENABLED`, `kagentUrl`) |
