# GitOps MCP — Argo CD / Kargo / Flux CD for agents

One MCP domain (`/mcp/gitops` on devai-api) and one adapter family
(`src/devai/adapters/gitops/`) give every DevAI agent first-class GitOps
operations across all three controllers, behind the registry-driven MCP Hub.

## Architecture

```
agents / MCP Hub / external MCP clients
        │ streamable-http (authMode: jwt)
        ▼
devai-api  /mcp/gitops            ← mcphub/tool_server.py: gitops_domain()
        │ tool registry bind (argocd_* kargo_* flux_*)
        ▼
adapters/gitops/  GitOpsAdapter ABC
   ├─ argocd.py   Application CRDs (wraps services/argocd.py)
   ├─ kargo.py    kargo.akuity.io: projects/stages/freight/promotions
   ├─ flux.py     {kustomize,helm,source}.toolkit.fluxcd.io
   └─ noop.py     tests / disabled / degrade
        │ kubectl (pod service account, CRD RBAC only)
        ▼
Kubernetes API
```

No controller API servers, no extra CLIs, no extra credentials: everything is
CRD reads/patches through the pod's service account. The required RBAC lives
in `tesserix-k8s/charts/apps/devai-api/templates/clusterrole.yaml`
(argoproj.io, kargo.akuity.io incl. the `promote` verb on Stages, and the
three fluxcd.io groups).

## Tools (18)

| Provider | Read | Mutating (gated + audited) |
|---|---|---|
| Argo CD | `argocd_list_apps`, `argocd_get_app`, `argocd_app_history`, `argocd_wait_healthy` | `argocd_sync`, `argocd_rollback` |
| Kargo | `kargo_list_projects`, `kargo_list_stages`, `kargo_get_stage`, `kargo_list_freight`, `kargo_list_promotions` | `kargo_promote` |
| Flux | `flux_list_kustomizations`, `flux_list_helmreleases`, `flux_get_object`, `flux_list_sources` | `flux_reconcile`, `flux_suspend` |

Every mutating call is logged at WARNING with agent/run/user attribution and
honors the platform gate. A failed backend answers with a readable
`{"ok": false, "error": ...}` — never an exception into the agent loop.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `DEVAI_GITOPS_PROVIDER` | `argocd` | Backend for the single-adapter path |
| `DEVAI_GITOPS_MCP_PROVIDERS` | `argocd,kargo,flux` | Backends the MCP domain exposes |
| `DEVAI_GITOPS_MUTATIONS_ENABLED` | `true` | Master gate for sync/promote/rollback/reconcile/suspend |
| `DEVAI_KARGO_DEFAULT_PROJECT` | `""` | Project assumed when a Kargo call omits one |
| `DEVAI_FLUX_NAMESPACE` | `flux-system` | Default namespace for Flux objects |

## Registry artifacts

- `architecture/registry-seeds/mcp-servers/gitops-mcp.yaml` — the MCPServer
  the Hub federates (endpoint `…/mcp/gitops`, `authMode: jwt`).
- `architecture/registry-seeds/tools/gitops-*.yaml` — 18 Tool seeds with real
  input schemas, labelled `mcp.devai.io/server: gitops-mcp`,
  `devai.io/domain: gitops`.
- Agents (generated from `specializations/`):
  - **deployment_engineer** — rollout verification + diagnosis + rollback
    (Argo CD + Flux), `risk_level: high`.
  - **release_promoter** — Kargo stage-by-stage promotion train,
    `risk_level: critical` (prod promotions hard-gate for human approval).
  - **gitops_auditor** — read-only drift/hygiene sweep across all three
    controllers, `risk_level: low`.
  - **release_manager** — upgraded with the full GitOps toolset and
    controller-aware deploy guidance.

## Per-user / per-tenant connections (Settings)

Five multi-instance connector families let a user wire their own
infrastructure — all stored with secrets in **that user's** GCP SM scope
(`devai-user-<uid>-<family>-<instance>-<field>`), only a SecretRef in
Postgres, resolution scope-isolated (user→team→tenant→global):

| Family | Consumed by |
|---|---|
| **Kubernetes Cluster** | every gitops tool's `cluster` arg → kubectl-against-your-cluster (`adapters/gitops/base.cluster_kubectl_flags`) |
| **Argo CD** (API mode) | argocd_* tools' `argocd` arg → `adapters/gitops/argocd_api` (REST) |
| **Kargo** (API mode) | kargo_* tools' `kargo` arg → `adapters/gitops/kargo_api` (Connect-RPC) |
| **Cloud Account** (gcp/aws/azure) | `cloud_*` tools → `adapters/cloud` |
| **MCP Server** | the Hub federates them per-caller (below) |

Identity flows through `ToolContext.triggered_by` — set by the agentruntime
runner **and** the legacy `ToolDispatcher` — so a tool call resolves the
calling user's own connection. Calls with no user identity (MCP-hub service
calls) safely fall back to the platform default. Policy knob
`DEVAI_GITOPS_REQUIRE_CLUSTER_CA` forbids `--insecure-skip-tls-verify` when a
connected cluster has no CA cert.

## Per-user MCP federation (the Hub)

`mcphub/personal.py` federates each caller's **own** connected MCP servers
into the Hub aggregate, namespaced `usr-<instance>__<tool>` and visible only
to that user (`MCPHub.list_tools_for(email)` / `call_tool(..., email=)`,
keyed off the principal the ASGI middleware terminated). User endpoints are
SSRF-screened with a public-host-allowing guard (block loopback/link-local/
private/metadata + DNS-rebinding; allow public). Legs connect on demand, are
cached per email for 120s, and carry the user's token on their chosen header
(`Authorization: Bearer …` or a custom `x-api-key`-style header).

## MCP Marketplace

`GET /api/settings/mcp/marketplace` lists every registry MCP server
(`connectable` vs always-on `builtin`); the dashboard Settings page renders a
browse-and-connect grid that pre-fills the MCP connector form.
