# DevAI ↔ aregistry ↔ agentgateway — End-to-End Integration

This document describes how a user-triggered task in DevAI fetches the right agent
definition from the **Agent Registry**, dispatches it through the **Agent Gateway**,
runs it as a Kubernetes Job, and uses the **A2A (Agent-to-Agent) protocol** for
inter-agent handoffs.

Publishing and release operations are covered separately in
[Publishing DevAI agents to the Agent Registry](AGENT-REGISTRY-PUBLISHING.md).

Diagrams are provided in two formats:

- **Mermaid blocks** below render natively on GitHub / GitLab / VS Code.
- **`.drawio` files** in `diagrams/` open directly in [drawio](https://app.diagrams.net) /
  the VS Code Drawio extension — use these to edit the diagrams.

---

## 1. Component topology

Six boxes, one user. That's it.

```mermaid
flowchart LR
    User([User])
    DevAI[DevAI API<br/>blueprint executor<br/>+ dispatcher]
    Reg[Agent Registry<br/>catalog of<br/>agents · skills · MCP · prompts]
    Runner[Runner Pod<br/>ephemeral K8s Job]
    AIGW[AI Gateway<br/>LLM proxy<br/>Anthropic · OpenAI]
    AGW[Agent Gateway<br/>MCP dispatch<br/>solo.io]
    LLM[/Anthropic · OpenAI/]
    MCP[/MCP servers/]

    User    -- 1. trigger --> DevAI
    DevAI   -- 2. get_agent --> Reg
    Reg     -. profile .-> DevAI
    DevAI   -- 3. create Job<br/>(profile in env) --> Runner
    Runner  -- 4. LLM call --> AIGW
    Runner  -- 5. MCP call --> AGW
    AIGW    --> LLM
    AGW     --> MCP
    Runner  -. 6. RESULT:: .-> DevAI

    style DevAI fill:#2d3a8c,color:#fff
    style Reg   fill:#7a3d8c,color:#fff
    style Runner fill:#3d5a3d,color:#fff
    style AIGW  fill:#8c5a2d,color:#fff
    style AGW   fill:#8c5a2d,color:#fff
```

### Component roles

| Component | Namespace | Port | Job |
|---|---|---|---|
| **devai-api** | `devai` | 8080 | FastAPI app: trigger handlers, blueprint executor, JobRunnerStage dispatcher |
| **devai-runner** | `devai` | — | Ephemeral Job pods. One per stage. Reads `DEVAI_AGENT_PROFILE` from env. |
| **devai-dashboard** | `devai` | 3100 | Next.js UI |
| **auth-bff** | `devai` | 8090 | Terminates OAuth, stamps `X-Forwarded-User/Uid/Tenant` |
| **agentregistry (aregistry)** | `agentregistry-system` | 12121 | Catalog. `/v0/{agents,skills,prompts,servers,health}`. pgvector for semantic search. |
| **agentgateway** | `agentgateway-system` | 9092 | solo.io MCP dispatch gateway. Routes `/mcp/{name}` → backend MCP server. |
| **ai-gateway** | `agentgateway-system` | 8080 | nginx LLM proxy. Routes `/anthropic/*` + `/openai/*` to public APIs, injects API keys server-side. |
| **kagent** | `kagent-system` | 8083 | Agent lifecycle controller (solo.io). Reconciles agents labelled `devai.io/runtime=kagent` into Deployments; the dispatcher routes those over A2A (all other agents stay Job-dispatched). |

→ Edit: [`diagrams/01-component-topology.drawio`](diagrams/01-component-topology.drawio)

---

## 2. The "spin up an agent" flow

Five clean steps from user click to running agent.

```mermaid
flowchart LR
    S1[STEP 1<br/>Dashboard sends trigger<br/>identity = alice@x.com<br/>trace = trace-abc]
    S2[STEP 2<br/>DevAI asks aregistry<br/>for the profile<br/>→ image, skills, prompts,<br/>mcp_servers, model]
    S3[STEP 3<br/>DevAI creates Job<br/>profile baked into env:<br/>DEVAI_AGENT_PROFILE,<br/>DEVAI_TRIGGERED_BY,<br/>DEVAI_AGENTGATEWAY_URL]
    S4[STEP 4<br/>Runner executes<br/>LLM → ai-gateway<br/>MCP → agentgateway<br/>writes RESULT:: stdout]
    S5[STEP 5<br/>DevAI parses RESULT,<br/>persists A2A,<br/>dashboard renders]

    S1 --> S2 --> S3 --> S4 --> S5

    style S1 fill:#2d3a8c,color:#fff
    style S2 fill:#7a3d8c,color:#fff
    style S3 fill:#3d5a3d,color:#fff
    style S4 fill:#8c5a2d,color:#fff
    style S5 fill:#5a5a5a,color:#fff
```

> **Key insight:** the aregistry call happens **once** per dispatch (step 2).
> The runner reads its profile from env vars baked into the Job by DevAI in step 3 —
> it does **not** re-query aregistry on boot.

### What each step gets right

| Step | Source-of-truth | Code |
|---|---|---|
| Identity at boundary | auth-bff `X-Forwarded-User` | `services/auth-bff/internal/proxy/proxy.go:92-95` |
| Identity in pipeline | `Principal` on `ALMState` / `DevAITask` | `src/devai/identity.py`, `src/devai/graph/state.py:62-70` |
| Agent profile resolution | aregistry single round-trip per dispatch | `src/devai/pipeline/stages/job_runner.py:_fetch_agent_profile` |
| Image selection | per-stack → profile → default | `_resolve_image` |
| Profile delivery to runner | `DEVAI_AGENT_PROFILE` env | `src/devai/runtime/job_spec.py:96-128` |
| Profile consumption | env-first, aregistry only on env miss | `src/devai/runner/entrypoint.py:_decode_agent_profile_from_env` |
| LLM dispatch | ai-gateway (keys injected server-side) | `DEVAI_ANTHROPIC_BASE_URL` in `values-prod.yaml` |
| MCP dispatch | agentgateway when `DEVAI_AGENTGATEWAY_URL` set; direct otherwise | `_resolve_mcp_endpoints` |
| Job completion | structured `RESULT::` line parsed from stdout | `_parse_runner_result` |

→ Edit: [`diagrams/02-spinup-flow.drawio`](diagrams/02-spinup-flow.drawio)

---

## 2a. How DevAI knows which agent and which MCP to use — in plain English

There are three questions to answer, and **three different things answer them**.
Each one has a single owner; they never overlap.

### Question 1 — "Which agent should run at this step?"

**Answered by the blueprint.** A blueprint is a YAML file in `blueprints/` that
lists the steps of a workflow in order. Each step names the agent that should
run there.

```yaml
# blueprints/app-scaffold.yaml
- name: scaffold_app
  stage: run_as_job
  config:
    agent: senior_developer    # ← the blueprint picks the agent
```

When DevAI walks the blueprint and reaches `scaffold_app`, it just reads
`config.agent` and gets the string `"senior_developer"`. That's it. The
blueprint author decides this at design time — DevAI doesn't choose, it obeys.

### Question 2 — "What does that agent need to do its job?"

**Answered by the Agent Registry (aregistry).** Aregistry is a catalog. For
every agent, it stores a record like a business card:

| Field | Example |
|---|---|
| name | senior-developer-agent |
| image | `ghcr.io/tesserix/devai/devai-runner:main` |
| model | anthropic / claude-sonnet-4 |
| skills | code_review, git_workflow, testing |
| prompts | implement_story, refactor |
| **mcp_servers** | **scm-mcp, devai-mcp** |

DevAI calls aregistry once per dispatch:

```
DevAI → aregistry: "what's on senior_developer's card?"
aregistry → DevAI: { …the whole business card… }
```

DevAI takes the whole card, **JSON-encodes it**, and writes it into a single
environment variable on the K8s Job: `DEVAI_AGENT_PROFILE`. That way the
runner pod gets everything it needs the moment it boots, without having to
phone aregistry again.

### Question 3 — "Where do those MCP servers actually live?"

**Answered by aregistry again.** The agent's card lists MCP server *names*,
not URLs. The runner asks aregistry for each name:

```
runner → aregistry: "where is scm-mcp?"
aregistry → runner: "http://devai-api.devai.svc.cluster.local:8080/mcp/scm"
```

Then there's one last twist: if `DEVAI_AGENTGATEWAY_URL` is set in the
runner's environment, the runner *replaces* the URL aregistry handed back
with one that goes through the Agent Gateway instead:

```
http://agentgateway.agentgateway-system.svc.cluster.local:9092/mcp/scm-mcp
```

That way traffic policy, retries, and tracing live on the gateway — but the
**discovery** is still aregistry's job.

### One-paragraph summary

> The blueprint picks **which agent** runs at each step.
> The agent's card in aregistry says **what skills, prompts, MCP servers, and
> model** that agent needs.
> Aregistry also tells the runner **where each MCP server actually lives**.
> The Agent Gateway just *reroutes* MCP traffic when it's enabled — it doesn't
> pick anything.

So:

> **Blueprint = which agent.
> Aregistry = what that agent needs and where to find it.
> Agent Gateway = how the traffic flows once decided.**

### Worked example — `senior_developer` on a Next.js scaffold

1. Blueprint `app-scaffold.yaml` says step `scaffold_app` runs
   `agent: senior_developer`.
2. DevAI asks aregistry for senior_developer's card → gets back model + skills
   + prompts + `mcp_servers: [scm-mcp, devai-mcp]`.
3. DevAI picks a runner image — for this step, the per-stack default
   (`devai-runner-nextjs:main`) wins over what's on the card.
4. DevAI creates a K8s Job. The Job's env carries the full card as JSON, plus
   the gateway URL.
5. The runner pod boots, reads the card from env (no aregistry call), and
   asks aregistry for each MCP server's address.
6. If a gateway URL is set, the runner rewrites those addresses to go through
   the Agent Gateway.
7. The agent runs: LLM calls flow through the AI Gateway, MCP calls through
   the Agent Gateway (or direct if the gateway URL is empty), Git commits go
   straight to GitHub.
8. The pod prints `RESULT::{...}` and exits. DevAI picks up the result and
   moves to the next blueprint step.

### Who changes what?

| If you want to… | Edit… |
|---|---|
| Add a new step to a workflow | a blueprint YAML in `blueprints/` |
| Swap which agent runs at a step | the `config.agent` field of that blueprint step |
| Give an agent access to a new MCP server | the agent's seed in `architecture/registry-seeds/agents/` |
| Add a new MCP server entirely | drop a YAML in `architecture/registry-seeds/mcp-servers/` |
| Route all MCP traffic through the gateway | set `DEVAI_AGENTGATEWAY_URL` in `tesserix-k8s` |

---

## 3. A2A protocol — agent-to-agent handover

DevAI agents collaborate via a small typed protocol. Six message types
(`request`, `response`, `notification`, `handoff`, `escalation`, `broadcast`)
flow on a per-run **A2A bus** that's just a list on `ALMState["a2a_messages"]`.
Every message carries the originating user (`triggered_by`) and a `trace_id`
so audit can answer "who asked for this and what work was correlated".

### Message envelope

```json
{
  "id": "01H...",
  "from_agent": "senior_developer",
  "to_agent":   "staff_reviewer",
  "message_type": "handoff",
  "subject": "Story #3 ready for review",
  "body":    "PR #42 implements the pagination API.",
  "payload": {"pr_number": 42, "story_index": 3},
  "in_reply_to": null,
  "timestamp": "2026-05-26T10:42:11Z",
  "triggered_by": "alice@x.com",
  "trace_id":     "trace-abc"
}
```

### Handover flow

```mermaid
sequenceDiagram
    participant EM as Engineering Manager
    participant SD as Senior Developer
    participant SR as Staff Reviewer

    EM->>SD: handoff   "implement story"
    SD-->>EM: notification   "branch created"
    SD->>SR: handoff   "PR #42 ready for review"
    SR-->>SD: response   "rename FooClient"
    SD-->>SR: response   "renamed in commit abc"
    SR-->>SD: response   "approved"
```

All six message types — `request`, `response`, `handoff`, `notification`,
`escalation`, `broadcast` — flow through the same `A2ABus.send()`. Every
message is persisted to Redis at `devai:run:{run_id}:a2a_messages` with
a 30-day TTL so the audit trail can be replayed.

### How identity propagates onto every message

The A2A bus is constructed *once per agent* with the run-level identity:

```python
# src/devai/core/base_agent.py:124-141
principal     = state.get("principal") or {}
triggered_by  = state.get("trigger_actor") or principal.get("email", "")
trace_id      = state.get("trace_id", "")
a2a = A2ABus(self.name, state.get("a2a_messages", []),
             triggered_by=triggered_by, trace_id=trace_id)
```

…and `send()` stamps both fields on every outbound message:

```python
# src/devai/graph/a2a.py:send
msg = {...}
if self.triggered_by: msg["triggered_by"] = self.triggered_by
if self.trace_id:     msg["trace_id"]     = self.trace_id
```

Result: an individual agent's code calls `a2a.handoff(...)`, `a2a.request(...)`,
etc. and **never has to remember to set identity** — the bus inherits it from
the state.

→ Edit: [`diagrams/03-a2a-protocol.drawio`](diagrams/03-a2a-protocol.drawio)

---

## 4. Identity propagation in full

Six hops from sign-in to attributed agent action.

```mermaid
flowchart TB
    A[1. alice@x.com signs in<br/>Google or Keycloak OAuth]
    B[2. auth-bff mints session cookie<br/>uid, email, tenant, pool]
    C[3. X-Forwarded-User stamped<br/>on every backend request]
    D[4. DevAI builds Principal,<br/>stamps onto ALMState]
    E[5. K8s Job env carries<br/>DEVAI_TRIGGERED_BY + DEVAI_TRACE_ID]
    F[6. Runner inherits identity<br/>on every A2A message + SCM commit]

    A --> B --> C --> D --> E --> F

    style A fill:#cccccc
    style B fill:#5a5a5a,color:#fff
    style C fill:#5a5a5a,color:#fff
    style D fill:#2d3a8c,color:#fff
    style E fill:#3d5a3d,color:#fff
    style F fill:#3d5a3d,color:#fff
```

The result: every agent action and every A2A message is attributable to
the originating user. Audit replay by `triggered_by` or `trace_id` works
end-to-end.

→ Edit: [`diagrams/04-identity-flow.drawio`](diagrams/04-identity-flow.drawio)

---

## 5. End-to-end smoke test (what "working" looks like)

The integration is healthy when **every one** of these holds:

| # | Check | How to verify |
|---|---|---|
| 1 | aregistry reachable | `kubectl exec -n agentregistry-system deploy/agentregistry -- curl -s http://localhost:8080/v0/health` returns `{ok:true}` (aregistry is internal-only — no public hostname) |
| 2 | dashboard shows correct counts | `/dashboard/registry` shows 8 tiles populated |
| 3 | trigger a run as alice@x.com | `POST /api/pipeline/trigger` returns `triggered_by:"alice@x.com"` |
| 4 | dispatch fetches profile | API logs: `registry.get_agent(senior_developer)` returns full record |
| 5 | Job env carries profile | `kubectl describe job <name>` shows `DEVAI_AGENT_PROFILE` non-empty |
| 6 | Runner uses profile, not re-fetch | runner logs: `agent profile resolved … from env` |
| 7 | LLM call hits ai-gateway | nginx access log on `ai-gateway` shows `/anthropic/v1/messages 200` |
| 8 | MCP call hits agentgateway when set | runner logs: `mcp endpoints routed_via=agentgateway` |
| 9 | A2A messages carry identity | `redis-cli LRANGE devai:run:<id>:a2a_messages 0 -1 \| jq '.[0].triggered_by'` → `"alice@x.com"` |
| 10 | Dashboard timeline attributes | `/runs/<id>` Timeline tab shows alice@x.com on each message |

If any of 1-10 regresses, the chain breaks silently — runs still complete, but the
identity / control-plane wiring degrades to "system" attribution.

---

## 6. Open work

| Status | Item |
|---|---|
| ✅ Done | **kagent dispatch (opt-in, per-agent).** `JobRunnerStage` routes an agent over A2A through the kagent controller when its registry record carries `devai.io/runtime=kagent` and `DEVAI_KAGENT_URL` is set; otherwise it dispatches a K8s Job. Definite pre-acceptance failures use the Job path; accepted or ambiguous outcomes fail closed to prevent duplicate tool calls. `kagent-agent-sync` reconciles the same labelled agents into Deployments. See `src/devai/pipeline/stages/job_runner.py::_maybe_dispatch_kagent` + `src/devai/agentic/kagent_client.py` |
| ✅ Done | **Dynamic on/off switch.** kagent is a `kagent` connector in the Settings catalog (`settings/models.py`, provider `on`/`off` → `kagent_enabled`, plus optional controller URL/namespace fields). Resolved per run through `build_overlay` with full scope precedence (user → team → org → tenant → global → `DEVAI_KAGENT_ENABLED` base), so toggling it lands on the next run with **no restart**. The Settings UI renders it automatically from the catalog. `JobRunnerStage._kagent_settings` reads the overlay before dispatch. |
| ⚠️ Caveat | Confirm the kagent controller's A2A `message/send` contract in-cluster before labelling a hot-path agent — the result-shape parsing in `extract_a2a_text` is defensive but unverified against the live 0.9.7 controller |
| ⚠️ Deferred | **agentgateway provider backends.** The gateway is in the topology but solo.io's MCP routing rules are not configured for our specific MCP servers — currently `routed_via=agentgateway` produces a URL that resolves on path but agentgateway's MCP backend resolution is still TODO in `tesserix-k8s/charts/apps/agentgateway` |
| ✅ Done | Aregistry profile injection into Job env (this PR) |
| ✅ Done | Identity propagation end-to-end (prior PR) |
| ✅ Done | MCP endpoint resolver with agentgateway fallback (this PR) |
