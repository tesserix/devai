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

## 5.5 Tool resolution & pull-through cache (the core loop)

The declarative promise: **an MCP is just a YAML list of tools/capabilities +
labels/tags — you never hand-author tool schemas.** When the operator binds an
MCP, every referenced tool is *resolved* through a three-tier chain, and anything
fetched from the internet is **cached back into the registry** so the next resolve
is instant. This is a **pull-through cache for capabilities** — the same shape as
the `ghcr-remote` image cache this platform already runs, and the formalization of
how the 1,103 community skills were imported from officialskills.sh (now automatic).

### Declarative MCP (what the user writes)
```yaml
kind: MCPServer
metadata:
  name: secops-mcp
  labels: { devai.io/category: security }
spec:
  displayName: SecOps MCP
  # Tools by explicit ref AND/OR by selector — no schemas inline.
  tools: [ security_scan_sast, trivy_scan, semgrep_scan ]
  toolSelector: { matchLabels: { devai.io/domain: security } }
  capabilities: [ sast, sca, secrets ]      # coarse discovery tags
  sources: [ registry, officialskills, mcp-registry, github ]  # optional override
```

### Resolution chain (per tool ref) — run by the operator
```
resolve(ref):
  1. REGISTRY  store.Get(kind=Tool, ref | selector)              hit → bind, done
  2. UPSTREAM  for src in sources (ordered, internet):           hit → step 3
                 officialskills.sh | mcp-registry | npm | github |
                 smithery | generic-http   → map to a Tool envelope
  3. CACHE     store.Apply(Tool{labels:{source:cache},           write-through
                 annotations:{cached-from:<url>, cached-at:<ts>, etag:<…>}})
               → bind the now-local Tool
  4. NOTIFY    miss everywhere → MCPServer.status.conditions:
                 Resolved=False reason=Unresolved
                 message="tool 'X' not in registry or any upstream — publish it
                          or add a source"  (+ event + dashboard banner)
               → the MCP STILL serves whatever resolved; never silently drop
```

### Source adapters (pluggable, per the CLAUDE.md adapter rule)
`internal/resolve/source.go`: `Source interface { Name() string; Resolve(ctx,
ref) (*Tool, error) }`. An ordered chain from config; `registry` (local store) is
always first, `noop` last. Each upstream maps an external tool/skill into a
`kind: Tool` envelope (schema + labels). Adding an upstream = one adapter + one
config entry — never a core change. This is the *exact* pattern used to pull the
community skills; it becomes a standing capability instead of a one-off script.

### Caching semantics
- **Write-through** on every upstream hit (`source: cache`, `cached-from`,
  `cached-at`, `etag`). Idempotent — registry upserts on (kind, ns, name), so
  concurrent resolves are safe.
- **Revalidate**: the operator periodically re-checks cached tools against upstream
  (TTL/etag); refresh on change, flag entries that vanished upstream.
- **Warm cache**: the first miss pulls from the internet and persists; every later
  MCP gets it immediately from the registry — no repeat fetch.

## 6. The DevAI MCP Hub — multiplexing done properly

The Hub (the **Agentic MCP**) is **one** MCP server to clients and an MCP **client**
to each downstream. Multiplexing here is NOT just merging tool-name lists — it's a
**stateful, protocol-complete 1↔N proxy**: one client session fans out to N
downstream sessions, *every* MCP primitive is aggregated, and server→client traffic
(notifications, sampling) is routed back. Getting this right is the whole point, so
it's specified in full.

### 6.1 What gets multiplexed (all MCP primitives — not just tools)
A correct mux federates the entire surface, each namespaced to avoid collisions:

| Primitive | client→server (forward) | name/uri rewrite |
|---|---|---|
| **Tools** | `tools/list`, `tools/call` | `analyst__security_scan_sast` |
| **Prompts** | `prompts/list`, `prompts/get` | `analyst__review_prompt` |
| **Resources** | `resources/list`, `resources/templates/list`, `resources/read`, `resources/subscribe` | URI prefixed `mcp+analyst://…` |
| **Completion** | `completion/complete` (arg autocomplete) | routed by ref's namespace |
| **Logging** | `logging/setLevel` | fan-out to all (or scoped) |

Merging only `tools` is the common, wrong shortcut — agents that use prompts/
resources would silently lose them.

### 6.2 Session & capability negotiation (`initialize`)
- The Hub runs its **own `initialize`** with each downstream (capability discovery)
  and a **single `initialize`** with the client. It advertises the **union** of
  downstream capabilities it can proxy (tools/prompts/resources/logging/completion).
- One **client session** (`Mcp-Session-Id`) maps to a **set of downstream sessions**
  (one per server). The Hub owns this fan-out table for the session's lifetime;
  downstream reconnects are transparent to the client.

### 6.3 Routing & correlation (the hard part)
- **Forward routing**: a namespaced request (`analyst__X`) → strip prefix → the
  `analyst` downstream session, via a `namespaced → (server, wire-name)` map per
  primitive.
- **JSON-RPC id mapping**: client and each downstream have independent id spaces;
  the Hub rewrites request ids and keeps a `clientId ↔ (server, downstreamId)` table
  to route responses back.
- **Progress + cancellation**: `progressToken`s are rewritten and correlated so
  `notifications/progress` from a downstream reaches the right client call;
  `notifications/cancelled` is forwarded to the owning downstream.

### 6.4 Reverse path (server→client fan-in) — must not be dropped
- **List-changed**: `notifications/{tools,prompts,resources}/list_changed` and
  `resources/updated` from any downstream → the Hub re-aggregates and emits a single
  `list_changed` to the client. (Also fired on registry/cache changes — §5.5.)
- **Sampling**: a downstream's **`sampling/createMessage`** (server asks the client's
  LLM to complete) is routed **up** to the client and the result back down. Skipping
  this breaks agentic downstream servers — it's the most-missed piece.
- **Elicitation / roots**: `elicitation/create` and `roots/list` are likewise
  proxied client↔downstream.
- **Logging**: `notifications/message` forwarded (tagged with the source server).

### 6.5 Scoping, budgeting, auth, degradation
- **Per-caller surface budgeting (the ~40-tool ceiling):** never expose the raw
  union. The Hub resolves a **`ToolProfile`** for the caller (from JWT scope /
  `?profile=` / an Agent's `allowed_tools`) — a label selector over Tools — and
  serves only that subset across all primitives. Default = `tier: core`.
- **Auth**: terminate the **caller's** JWT at the Hub (gateway/Keycloak `agentic`
  pool); inject each **downstream's** auth per `MCPServer.spec.authMode` (`jwt` →
  service token, `none`, `header`, `mtls`). Callers never hold downstream creds.
- **Degradation**: an unreachable/`degraded` downstream is dropped from the
  aggregate (its primitives disappear, a `list_changed` fires, in-flight calls to it
  error cleanly) — the rest keep serving. Never fail the whole Hub for one bad mux leg.
- **Concurrency**: bounded connection pool per downstream; `tools/list` fan-out runs
  in parallel with per-leg timeouts; slow/blocked legs can't stall the aggregate.

### 6.6 Transport & placement
- Streamable HTTP to clients, mounted at `/mcp` (replacing today's single FastMCP
  mount in `webhook/app.py`); downstream transport per `MCPServer.spec.transport`
  (streamable-http now; stdio via a runner adapter in Phase 4).
- Dedicated `devai-mcp-hub` Deployment (decision §9.2), discovery-driven by the
  registry (§4) — it never hardcodes a downstream.

**In one line:** the Agentic MCP is a registry-driven, capability-complete MCP
multiplexer — one endpoint, one auth, one negotiated session, fanning out to many
small MCPs and faithfully proxying every primitive in both directions, scoped per
caller.

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
| **2 — Selector + operator + resolver (registry tier)** | `spec.toolSelector` on the 5 MCPServers (done); reconciler validates, resolves selector → `.status.tools`, health-probes, and runs the resolution chain's **registry tier** with **NOTIFY** on miss (status condition + event). | Adding a labelled Tool auto-appears in its server's `.status.tools`; an unresolved tool surfaces a clear `Resolved=False` message; invalid specs warned in `.status`. |
| **3 — Upstream sources + pull-through cache** | `internal/resolve` Source adapters (`officialskills`, `mcp-registry`, `github`, `generic-http`); upstream hit → **write-through** `kind:Tool` (cache labels) → bind; periodic revalidate (etag/TTL). | A tool absent locally but present upstream is fetched, **persisted to the registry**, and served; second resolve is a registry hit; vanished-upstream entries flagged. |
| **4 — The Hub** | `devai-mcp-hub`: registry discovery, downstream federation, namespacing, profile budgeting, auth termination/injection. Replace the single `/mcp` FastMCP mount. | One `/mcp` lists a scoped, namespaced tool set; `tools/call` routes to the right downstream with correct auth; a downstream outage degrades gracefully. |
| **5 — Gateway + scale** | Route `…/mcp` → Hub via agentgateway; onboard ≥2 new external MCPs via registry only; `tools/list_changed` on registry/cache change. | New MCP onboarded with zero Hub code change; declaring a tool not yet in the registry auto-pulls + caches it; surface stays within the per-caller budget. |

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

**Standard we're setting (for others to follow):**
1. **Registry is the single source of truth.** Capabilities (tools) are
   first-class, labelled artifacts.
2. **Composition is label selectors**, not hardcoded lists (server→tools,
   profile→tools, hub→servers), reconciled by an operator into `.status`.
3. **Declarative + resolved + cached.** You declare an MCP as a YAML list of
   tools/labels; the operator *resolves* each through **registry → internet →
   notify**, and **pull-through-caches** any internet hit back into the registry —
   the registry becomes a warm cache, never a hand-maintained list.
4. **Runtime reads resolved status, never hardcodes topology** (the Hub federates
   what the registry says exists).
5. **Identity terminated once at the edge, injected downstream.**
6. **Degrade, never silently drop** — an unresolved tool or a dead downstream is a
   clear, actionable status message, not an empty result.

Add a capability by publishing (or merely *referencing*) it — never by editing code.

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
