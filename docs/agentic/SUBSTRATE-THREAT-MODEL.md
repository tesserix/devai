# DevAI user-authored Agent threat model

> **Status (2026-08-20): the Substrate Actor path is not approved for user
> traffic.** Production must keep `DEVAI_KAGENT_ENABLED=false` until every gate
> in this document passes. The supported on-demand Job and sandbox paths remain
> available with gateway-required provider routing.

This document defines the security boundary for running code and instructions
authored by a DevAI user. It covers the DevAI API, agentic registry, AgentGateway,
AI gateway, MCP services, kagent, and Agent Substrate. The production operations
and rollback procedure remain in [SUBSTRATE-SETUP.md](SUBSTRATE-SETUP.md).

## Security objective

A malicious Agent controlled by one authenticated principal must not be able to:

- obtain another user or tenant's connector, prompt, registry object, memory,
  trace, evaluation result, workspace, usage record, or cost allocation;
- use a platform, team, organization, or tenant provider credential through the
  Actor passthrough path;
- bypass AgentGateway or the AI gateway to call an MCP service or provider
  directly;
- access the Kubernetes API, node credentials, cloud metadata, workload
  identity, production SCM/cloud/GitOps credentials, or control-plane secrets;
- call a tool outside the server-approved allowlist or retain a destructive tool
  grant beyond the approved run; or
- consume another tenant's concurrency, token, or cost budget without attribution.

The boundary must continue to hold when the Agent prompt, generated code, tool
arguments, MCP response, model response, and workspace contents are hostile.

## Actors and attacker capabilities

| Actor | Trust level | Capabilities |
|---|---|---|
| Authenticated DevAI user | Untrusted tenant principal | Authors Agents, prompts, datasets, and sandbox requests; selects only visible connectors and tools |
| User-authored Agent | Hostile workload | Generates arbitrary model output and tool arguments; may attempt prompt injection, data exfiltration, persistence, resource exhaustion, or sandbox escape |
| DevAI API and auth BFF | Trusted control plane | Verifies the principal, derives tenant/user scope, resolves policy and connectors, and records attribution |
| Agentic registry | Trusted data plane | Enforces object visibility and ownership before export or mutation |
| AgentGateway and AI gateway | Trusted policy enforcement | Restrict destinations, sanitize headers, apply provider routes, and emit attributable usage telemetry |
| Approved MCP service | Partially trusted dependency | May return hostile content; receives only the scoped identity and arguments required for the approved tool |
| kagent and Substrate controllers | Trusted cluster control plane | Materialize runtime objects and dispatch to a WorkerPool; never accept user-selected pod security or service-account fields |
| WorkerPool process | Trusted runtime infrastructure | Starts gVisor sandboxes and multiplexes tenants; compromise is a node- and tenant-wide incident |
| Actor sandbox | Untrusted execution boundary | Runs one Agent workload with no production secret or Kubernetes identity |

An ordinary user cannot set WorkerPool images, runtime class, pod security,
ServiceAccounts, volumes, host paths, node placement, network policy, or gateway
policy. Those remain operator-owned GitOps state.

## Trust boundaries and data flow

```text
browser
  | authenticated request; no trusted tenant/user fields from the client
  v
auth BFF -> DevAI API -> owner-filtered registry/settings/storage
                         | server-derived tenant_id + user_scope_id
                         | confirmed USER-scope connector identifier
                         v
                    runtime dispatch
                      |                 |
             supported Job path   gated Actor path
                      |                 |
                      +---- AI gateway -+---- approved provider
                      |
                      +---- AgentGateway ---- approved MCP service
```

The following boundaries are distinct:

- `DEVAI_AGENTGATEWAY_URL` is the private MCP gateway base. It is not an AI
  provider URL and is not an authorization decision by itself.
- The AI gateway base maps every supported provider to a private gateway route.
  With `DEVAI_LLM_GATEWAY_REQUIRED=true`, a missing route fails closed rather
  than using a provider's public endpoint.
- The Job or Actor receives only the selected per-run credential. Connector
  records and secret-store access remain in the trusted DevAI control plane.
- Registry visibility is checked before runtime state is joined to an Agent.
  Runtime objects alone never prove that the caller owns an Agent.

## Required invariants

### Identity and tenancy

- Authentication establishes the principal; request bodies and forwarded user
  headers cannot choose the effective tenant or user.
- User scope is tenant-qualified. The same subject identifier in two tenants
  produces different registry, settings, memory, usage, sandbox, and cost keys.
- Cross-tenant object access returns the same not-found response as a missing
  object and does not expose owner, connector, controller, or condition data.
- Public platform artifacts may be read, but any derived run, evaluation,
  workspace, trace, or baseline remains owned by the invoking principal.

### Credentials

- The Actor A2A bearer path accepts only a connector whose source scope is
  `USER` and whose qualified owner matches the verified principal.
- Team, organization, tenant, global, platform, disabled, shared, and another
  user's connectors cannot cross the Actor bearer boundary.
- A sandbox requires an explicitly selected and confirmed personal LLM
  connector. SCM, cloud, GitOps, settings, memory, event-bus, and production
  workload-identity credentials are denied by default.
- Keyless Vertex in a sandbox or Actor must not borrow the DevAI API's GCP
  Workload Identity. Vertex platform Workload Identity is not a personal
  connector and must not be described as per-user key isolation.
- Secret values never appear in registry artifacts, Kubernetes metadata, audit
  records, errors, traces, analytics, or logs.

### Network and tools

- Actor egress is default-deny. Allowed traffic is limited to DNS, the private
  AI gateway, AgentGateway, and explicitly approved control-plane endpoints.
- Cloud metadata, Kubernetes API, node-local services, private cluster ranges,
  and direct public provider/MCP endpoints remain denied.
- Provider adapters fail closed when the AI gateway route is missing.
- MCP calls use an explicit server and tool allowlist. Wildcards and grants over
  the controller's maximum tool count are rejected before publication.
- MCP output is untrusted input. It cannot change the principal, connector,
  destination allowlist, tool grant, or cost owner.

### Runtime and data

- Each untrusted workload executes in a kernel-isolated Actor or in the
  supported hardened Job/sandbox boundary. A container-only boundary is not
  sufficient for hostile user code.
- No platform secret, Kubernetes API token, cloud identity, host filesystem, or
  another Actor's writable state is mounted into an Actor.
- Durable state, snapshots, workspaces, traces, and evaluation results include
  tenant and owner keys, and authorization is checked again on every read.
- Actor reuse cannot reuse another principal's credential, request headers,
  memory, environment, filesystem, or pending tool call.
- Deleting an ephemeral sandbox never deletes its immutable evaluation and
  usage history.

### Quotas, cost, and audit

- Provider requests carry server-derived tenant, user, run, sandbox, Agent, and
  provider attribution after gateway header sanitization.
- Token and monetary limits are enforced before dispatch and updated from actual
  usage. The same subject in two tenants has disjoint rollups.
- Concurrency and rate limits are tenant-scoped so one user's run cannot exhaust
  every worker or provider allowance.
- Credential grants and privileged/destructive tool approvals are audited using
  identifiers only. Audit events are append-only to the acting principal.

## Threats, controls, and current evidence

| Threat | Required control | Current evidence | Status |
|---|---|---|---|
| Cross-user connector reuse | Qualified owner lookup and USER-scope-only Actor passthrough | Resolver, sandbox credential, and kagent dispatch negative tests | Pass on application paths |
| Provider bypass | Gateway-required provider mapping with no direct fallback | All supported provider mapping and missing-route tests; production flag is true | Pass on Job/sandbox paths |
| MCP bypass | Private AgentGateway URL plus explicit RemoteMCPServer/tool allowlist | Registry export/reconcile tests and production configuration | Pass on supported path |
| Cross-tenant registry/data read | Owner filtering before object/runtime lookup; generic 404 | Registry tenancy, identity, eval, sandbox, and usage tests | Pass on application paths |
| Secret leakage into sandbox | Confirmed personal LLM grant only; production extras stripped | Sandbox credential and Job-spec tests | Pass in tests; signed-in owner acceptance remains #194 |
| Kubernetes API credential theft | Dedicated tokenless WorkerPool identity and no Actor token | Local v0.0.15 controller patch proves a tokenless per-pool ServiceAccount; no published artifact | Fail in production runtime |
| Actor escape into WorkerPool/node | Kernel isolation, hardened trusted worker, dedicated sandbox nodes | v0.0.15 uses gVisor, but the worker is root with hostPath, broad capabilities, and AppArmor Unconfined | Fail |
| Direct or lateral network exfiltration | Default-deny Actor/WorkerPool policies and explicit gateway/control-plane allows | Current policies are not yet a proven least-privilege production boundary | Fail |
| Shared Actor state after reuse | Per-run reset plus cross-principal negative test | No production Actor reuse test while the runtime is disabled | Not proven |
| Cost theft or noisy neighbor | Tenant/user/run attribution, budgets, and measured WorkerPool capacity | Job/sandbox ledger tests pass; Actor 5/20/50 measurements do not exist | Partial |

## Mandatory negative tests

Before enabling the Actor path, retain machine-readable evidence for all of the
following. Each test uses two principals with the same subject identifier in
different tenants where applicable.

1. Principal A cannot list, fetch, edit, unpublish, dispatch, resume, or observe
   principal B's private Agent, prompt, MCP server, sandbox, workspace, trace,
   evaluation, baseline, memory, snapshot, usage, or cost record.
2. An Actor triggered by A cannot receive B's user connector or any team,
   organization, tenant, global, platform, or keyless Vertex credential.
3. A malicious prompt, model response, or MCP response cannot override gateway
   routing, destination allowlists, attribution headers, or tool policy.
4. Actor egress to the Kubernetes API, cloud metadata, node addresses, another
   WorkerPool pod, another Actor, and direct provider endpoints is denied.
5. The Actor pod or process contains no Kubernetes token, production secret,
   cloud identity, host mount, or writable cross-run volume.
6. Actor teardown and reuse leave no previous environment, credential, header,
   file, process, socket, memory, or tool-call state observable by the next run.
7. Token, cost, and concurrency consumption is charged only to A's qualified
   tenant/user/run and cannot reduce B's budget.
8. WorkerPool/controller unavailability detected before dispatch degrades to the
   supported Job path. A timeout or error after possible acceptance fails closed
   without retry amplification or credential reuse.

Tests must assert generic client errors and inspect only metadata or hashes.
They must never print a credential, secret payload, Kubernetes token, or user
content.

## Current NO-GO and rollout gates

Production currently runs Substrate 0.0.8 with an unready keyless canary. A local
compatibility experiment with Substrate 0.0.15 reached a Ready gVisor Actor only
after three unpublished kagent fixes. The v0.0.15 trusted worker still runs as
UID/GID 0, mounts `/var/lib/ateom-gvisor` from the host, uses AppArmor
`Unconfined`, and adds capabilities including `SYS_ADMIN`, `NET_ADMIN`,
`SYS_PTRACE`, and `NET_RAW`.

The Actor path remains a NO-GO until all of these are true:

1. A published, digest-pinned kagent release targets the selected Substrate
   release without local compatibility patches.
2. Every WorkerPool uses a dedicated tokenless ServiceAccount with no cloud
   workload identity or RBAC beyond its proven runtime requirement.
3. The trusted worker design and dedicated sandbox node boundary are reviewed
   for the remaining root, hostPath, AppArmor, and capability requirements.
4. Default-deny ingress and egress pass every negative test above.
5. The keyless canary is Accepted and Ready, survives controller and worker
   restarts, and completes wake-and-return without stale Actor state.
6. The 5/20/50 Actor test records CPU, memory, pod count, failures, queue depth,
   p50/p95 cold-start and run latency, and impact on core DevAI workloads.
7. A signed-in owner acceptance proves personal-connector selection, absence of
   SCM/cloud credentials, identifier-only audit, and correct usage attribution.
8. Migration, protected signing state, rollback, and named production approval
   are recorded before the GitOps promotion.

There is no Actor availability or latency SLO until those measurements exist.
The current service objective is a Job selection before Actor acceptance and an
explicit uncertain result afterward. Availability must not be bought by executing
the same side-effecting task twice.

## Incident boundary

Treat any unexpected WorkerPool privilege, network path, mounted token, Actor
state reuse, attribution mismatch, or cross-tenant existence signal as a
security incident. Keep `DEVAI_KAGENT_ENABLED=false`, preserve identifiers and
timestamps without payloads, rotate any credential that may have crossed the
boundary, and use the GitOps rollback in the production runbook. Do not repair
state imperatively or delete WorkerPool, database, signing, snapshot, or PVC
state without the named approval and recovery capture required by the runbook.
