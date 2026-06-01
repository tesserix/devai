# DevAI MCP Hub — registry-driven MCP multiplexing + first-class Tools

**Status: design (for review).** Supersedes the host/client sketch in
[`docs/plans/cursor-parity/08-mcp-host.md`](../plans/cursor-parity/08-mcp-host.md)
with a concrete, registry-driven architecture. Spans three repos:
`agentic-registry` (typing + operator), `devai` (the hub), `tesserix-k8s` (deploy).

---

## 1. Problem

The aregistry UI shows an empty **Tools** tab, and there's no coherent way to grow
the MCP surface. Concretely:

- **`Tool` is a registry kind** (`/v0/tools`) but **no `Tool` artifacts exist**.
  Tools live in two disconnected places:
  1. Python defs in `src/devai/tools/registry.py` — real `RegisteredTool{name,
     description, parameters(JSON Schema)}`.
  2. **Bare strings** inside each `MCPServer.spec.tools` (e.g. `analyst-mcp` →
     `security_scan_sast`, `validate_compile`, …).
  Neither publishes a `Tool` artifact → `/v0/tools` is empty, and there is no
  schema, no labels, no binding between a tool and the server that serves it.
- **Hardcoded tool lists** on each MCPServer: adding a tool means editing the
  server's seed. There's no dynamic attachment.
- **No aggregation.** Today there are 5 MCP servers (analyst 16, scm 15, sre 13,
  devai 7, sample 2 = ~53 tools) each on its own endpoint. As we add more, a
  client must connect to each one. There's no single DevAI MCP surface.
- **The tool-budget ceiling** (from plan 08): an agent practically tops out around
  ~40 active tools before tool definitions blow the context budget. A naive union
  of every server's tools does not scale — the surface must be *budgeted and
  scoped per caller*.

## 2. Goals / non-goals

**Goals**
- Make **Tools first-class** registry artifacts with schema → the Tools tab lists
  them; tools carry labels/annotations.
- **Dynamic attachment**: an MCPServer selects its tools by **label selector**, not
  a hardcoded list. Add a labelled Tool → it auto-attaches.
- A single **DevAI MCP Hub**: one `/mcp` endpoint that **multiplexes** all the
  smaller MCP servers, namespaces their tools, terminates auth, and is **driven by
  the registry** (no hardcoded servers).
- **Scoped tool surface per caller** (respect the ~40 ceiling) via profiles/RBAC.
- Clean integration with the **agentgateway** (ingress/mTLS/JWT) and the
  **registry** (source of truth).

**Non-goals (for now)**
- Replacing per-server MCP endpoints (they stay; the hub federates them).
- A general-purpose service mesh for MCP (agentgateway already does ingress).
- Editor/IDE-specific transports beyond Streamable HTTP + stdio.

## 3. Architecture

```
   MCP clients (Claude, IDEs, devai agents, A2A)
          │  ONE endpoint, ONE auth (JWT via gateway)
          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  DevAI MCP Hub   (devai-mcp-hub, exposes /mcp)             │
   │  • client-auth termination (validates caller JWT)          │
   │  • discovery: reads kind:MCPServer from the registry       │
   │  • federation: connects to each downstream MCP             │
   │  • namespacing: <server>__<tool>  (collision-free)         │
   │  • surface budgeting: scoped tool set per caller/profile   │
   │  • downstream auth injection per MCPServer.spec.authMode   │
   └───────────────┬──────────────────────────────────────────┘
                   │ source of truth + selectors            ▲ status
         ┌─────────▼──────────┐         ┌───────────────────┴────┐
         │  Agentic Registry  │ watch   │  mcp-operator           │
         │  kind: Tool         │◄───────►│  • validates schema     │
         │  kind: MCPServer    │  bind   │  • resolves toolSelector│
         └─────────┬──────────┘         │  • writes .status       │
   toolSelector (matchLabels) resolves  └─────────────────────────┘
   ┌──────────┬──────────┬──────────┬──────────┬──────────────┐
   │ analyst  │   scm    │   sre    │  devai   │  external…    │  small MCPs
   │  :16     │   :15    │   :13    │   :7     │               │
   └──────────┴──────────┴──────────┴──────────┴──────────────┘
        devai-api:8080/mcp/*            devai-sre:8090/mcp/*
```

**Flow:** a client connects to the Hub's `/mcp` (through agentgateway). The Hub
reads `kind:MCPServer` from the registry, connects to each downstream, lists their
tools, namespaces them, applies the caller's scope/profile, and presents one
`tools/list`. A `tools/call` for `analyst__security_scan_sast` is routed to the
`analyst` downstream with its configured auth.

## 4. Data model (registry)

The registry `spec` is freeform (`map[string]interface{}`), so these are
**conventions** the operator validates — no breaking schema change required.

### 4.1 `kind: Tool` (new first-class artifact)

```yaml
apiVersion: registry.agentic.dev/v1alpha1
kind: Tool
metadata:
  name: analyst-security-scan-sast        # globally unique slug
  namespace: devai
  visibility: internal                     # tools are tenant-internal by default
  labels:
    mcp.devai.io/server: analyst-mcp       # ← which MCP serves it (attachment key)
    devai.io/domain: security
    devai.io/risk-level: low
    devai.io/tier: core                    # core | extended | experimental (budgeting)
  annotations:
    mcp.devai.io/wire-name: security_scan_sast   # the name the downstream exposes
spec:
  displayName: SAST Scan
  description: Static application security testing; returns normalized Finding[].
  inputSchema:                             # JSON Schema (from ToolSpec.parameters)
    type: object
    properties:
      path: { type: string }
    required: [path]
  outputSchema:                            # optional, advisory
    type: object
    properties: { findings: { type: array } }
```

`inputSchema` comes directly from devai's `RegisteredTool.parameters` /
`register_schema_dicts({name, description, input_schema})`. One Tool artifact per
wire tool.

### 4.2 `kind: MCPServer` (gains `toolSelector`, keeps backward-compat)

```yaml
apiVersion: registry.agentic.dev/v1alpha1
kind: MCPServer
metadata:
  name: analyst-mcp
  namespace: devai
  labels: { devai.io/source: devai, devai.io/category: analyst }
spec:
  displayName: DevAI Analyst MCP
  endpoint: http://devai-api.devai.svc.cluster.local:8080/mcp/analyst
  transport: streamable-http              # streamable-http | sse | stdio
  authMode: jwt                           # jwt | none | header | mtls
  # NEW — tools are selected by label, resolved by the operator. The legacy
  # `tools: [..]` string list still works (treated as an explicit pin) so nothing
  # breaks during migration.
  toolSelector:
    matchLabels: { mcp.devai.io/server: analyst-mcp }
status:                                    # written by the operator
  tools: [analyst-security-scan-sast, …]   # resolved tool artifact names
  toolCount: 16
  health: ready                           # ready | degraded | unreachable
  lastProbe: "2026-06-02T…"
```

### 4.3 Label / annotation conventions

| Key | On | Meaning |
|---|---|---|
| `mcp.devai.io/server` | Tool | which MCPServer serves it (attachment key) |
| `mcp.devai.io/wire-name` (anno) | Tool | downstream's raw tool name (for routing) |
| `devai.io/domain` | Tool | security \| quality \| scm \| sre \| … (grouping) |
| `devai.io/tier` | Tool | core \| extended \| experimental (surface budgeting) |
| `devai.io/risk-level` | Tool/MCPServer | low \| medium \| high (gating) |
| `mcp.devai.io/profile` | MCPServer/Tool | named tool-surface profile membership |

## 5. The operator / reconciler (`mcp-operator`)

A small controller (runs in `agentic-registry` as a reconcile loop, or as a
sidecar). It is the thing that **holds the schema** and does the **dynamic
attachment** you asked for. Responsibilities:

1. **Validate** `Tool` and `MCPServer` specs against the conventions above
   (JSON-Schema on `inputSchema`, required fields, label hygiene). Reject invalid
   on apply via a registry admission hook (or surface in `.status`).
2. **Resolve `toolSelector` → `.status.tools`**: list `Tool` artifacts matching the
   selector (the registry already supports `labelSelector` lists), write the
   resolved set + count onto `MCPServer.status`, and the reverse pointer onto each
   `Tool.status.servers`. Add a labelled Tool → reconcile re-binds automatically.
3. **Health**: probe each MCPServer endpoint (`tools/list`), reconcile `.status.health`.
4. **Drift**: re-run on registry changes (watch) and on a timer.

This keeps the Hub dumb: it reads resolved `.status`, it doesn't compute bindings.

## 6. The DevAI MCP Hub (multiplexer)

A new service (`devai-mcp-hub`, or a mode of `devai-api`) that is the single MCP
surface. It is an **MCP server to clients** and an **MCP client to each downstream**.

- **Discovery**: on startup + on registry watch, list `kind:MCPServer` (RBAC-scoped)
  from the registry; for each `ready` server, open a downstream MCP client to its
  `endpoint` over its `transport`.
- **Federation + namespacing**: union the downstream tools, renaming each to
  `<server-shortname>__<wire-name>` (e.g. `analyst__security_scan_sast`) so two
  servers can both expose `validate_lint` without collision. Keep a routing map
  `namespaced → (server, wire-name)`.
- **Surface budgeting (the ~40 ceiling)**: never expose the raw union. The Hub
  resolves a **profile** per caller (from JWT scope / a `?profile=` / an Agent's
  `allowed_tools`) and serves only that subset. Profiles are themselves registry
  objects (label selectors over Tools), e.g. `profile: reviewer` → `domain in
  (security, quality)`. Default profile is `core` tier only.
- **Auth**: terminate the **caller's** JWT at the Hub (validated via the gateway /
  Keycloak `agentic` pool). Inject the **downstream's** auth per
  `MCPServer.spec.authMode` (`jwt` → mint/forward a service token; `none` → nothing;
  `header`/`mtls` → per config). Callers never hold downstream creds.
- **Live reconfig**: a registry change (new server, new tool, health flip) triggers
  a re-list; `tools/list_changed` is emitted to connected clients.
- **Degradation**: an unreachable downstream is dropped from the surface (logged),
  not fatal — the rest keep working.
- **Transport**: Streamable HTTP for clients (mounted at `/mcp`, same as today's
  FastMCP mount in `webhook/app.py`); the Hub replaces the single FastMCP app.

## 7. Registry + gateway integration

- **Registry = source of truth.** The Hub and operator read `MCPServer`/`Tool`/
  `Profile` artifacts. Nothing about the topology is hardcoded — onboard a new MCP
  by publishing an `MCPServer` (+ its `Tool`s), and it appears in the Hub.
- **agentgateway = ingress.** It already fronts `aregistry`/devai; route
  `aregistry.tesserix.app/mcp` (or `mcp.tesserix.app`) → the Hub, terminating
  mTLS + the Keycloak JWT. The Hub trusts the gateway's stamped identity headers
  (same pattern as `src/devai/identity.py`).
- **RBAC.** The Hub's discovery uses the caller's identity (`CanRead`-style) so a
  caller only sees servers/tools they're authorized for — the same visibility model
  my `v0List`/`v0Search` browse fix now relies on.

## 8. Phased implementation

| Phase | Deliverable | Acceptance |
|---|---|---|
| **1 — Tools first-class ✅ DONE** | `kind:Tool` artifacts generated from each MCPServer's tool list + devai `ToolSpec` schemas, labelled `mcp.devai.io/server`; `toolSelector` added to all 5 MCPServers. Generator: `_import/generate_tools.py`. | **Met:** `/v0/tools` → 53 (UI Tools tab populated); `labelSelector=devai.io/domain=security` → 6; each Tool has inputSchema + server label + wire-name annotation. |
| **2 — Selector + operator** | Add `spec.toolSelector` to the 5 MCPServers; build the reconciler (validate + resolve selector → `.status`, health probe). | Adding a labelled Tool auto-appears in its server's `.status.tools`; invalid specs are rejected/surfaced. |
| **3 — The Hub** | `devai-mcp-hub`: registry discovery, downstream federation, namespacing, profile budgeting, auth termination/injection. Replace the single `/mcp` FastMCP mount. | One `/mcp` lists a scoped, namespaced tool set; `tools/call` routes to the right downstream with correct auth; a downstream outage degrades gracefully. |
| **4 — Gateway + scale** | Route `…/mcp` → Hub via agentgateway; onboard ≥2 new external MCPs via registry only; `tools/list_changed` on registry change. | New MCP onboarded with zero Hub code change; surface stays within the per-caller budget. |

## 9. Decisions (locked)

These are settled to the best-standard choice so the build can proceed without
round-trips; revisit only if a phase surfaces a concrete reason.

1. **Operator placement → in-registry reconcile loop first**, extract to a
   standalone `mcp-operator` only if it needs an independent release cadence. One
   deploy, least moving parts; the registry already owns the store + watch.
2. **Hub placement → dedicated `devai-mcp-hub` Deployment**, sharing the `devai`
   image (reuses auth/identity/registry client) but isolated so an MCP outage or a
   chatty client can't take down `devai-api`. Independent HPA per task.
3. **Profiles → a governed `ToolProfile` artifact** (label-selector over Tools),
   not config — so the tool-surface budget is auditable/versioned like everything
   else and a profile change is a publish, not a redeploy.
4. **Schema enforcement → warn-first** (`.status` conditions on apply), harden to
   admission-time rejection once the catalog is clean. Never block seeding on a
   soft validation miss — the registry must degrade, not refuse.
5. **stdio downstream MCPs → subprocess/Job adapter, deferred to Phase 4.** HTTP
   (Streamable) first; stdio servers wrapped as a runner Job the Hub awaits.

**Standard we're setting (for others to follow):** *the registry is the single
source of truth; capabilities (tools) are first-class, labelled artifacts;
composition (server→tools, profile→tools, hub→servers) is expressed as
**label selectors**, reconciled by an operator into `.status`; the runtime (Hub)
reads resolved status and never hardcodes topology; identity is terminated once at
the edge and injected downstream.* Add a capability by publishing an artifact with
the right labels — never by editing code.

## 10. Why this is the right shape

- **Registry-driven** = one source of truth; onboarding an MCP is a publish, not a
  code change — which is exactly how skills/agents already work here.
- **Labels for attachment** = the same Kubernetes-style selector model the registry
  already supports (`labelSelector`), so tools attach to servers (and servers to
  profiles) declaratively and dynamically.
- **One Hub, namespaced + budgeted** = "many small MCPs → one big DevAI MCP" without
  hitting the tool-context ceiling, with collision-free names and per-caller scope.
- **Auth at the edge** = callers present one identity; downstream creds never leak;
  the gateway + Keycloak stay the trust boundary.
