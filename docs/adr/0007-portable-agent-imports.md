# ADR 0007: Portable agent imports and reproducible evaluation

## Status

Accepted for implementation by issue #316.

## Context

DevAI can already publish tenant-owned agents, create isolated sandboxes, record
traces, run versioned evaluations, compare runs, and gate promotion. Agentic
Registry can already version, sign, resolve, and semantically discover agentic
artifacts. What is missing is a durable boundary between an agent authored in
any framework and the exact runnable object DevAI evaluates.

Initial sizing assumes 10,000 tenants, 100,000 Registry artifacts, 50 peak
search requests per second, 10 imports per second, 20 concurrent sandboxes, and
threefold artifact growth over 36 months. Normal definitions are below 256 KiB;
executable layers remain in OCI registries. Registry search targets 99.9%
monthly availability and p99 below 300 ms. Import submission targets p99 below
500 ms before asynchronous image verification. Search may be five seconds
stale; an accepted import and its lock must be strongly consistent.

The assets are customer agent code, prompts, dependency metadata, model and tool
credentials, datasets, traces, and promotion authority. Threat actors include
an unauthenticated caller, another tenant, a compromised agent or adapter, a
malicious Registry artifact or OCI image, and an insider. Trust crosses the
browser or CLI, DevAI, Registry and OCI, the sandbox, and external model, tool,
and MCP endpoints. Every crossing validates schema and authorizes the concrete
tenant-owned object.

## Decision

### Portable contract

Registry `Agent` artifacts may declare `spec.definitionVersion: v1`. The known
v1 projection contains:

- framework identity and semantic capabilities;
- JSON input and output schemas;
- one primary runtime: signed immutable OCI plus A2A, or authenticated remote
  A2A;
- exact version or digest references to Skills, Tools, MCP servers, Prompts,
  Workflows, Datasets, and EvalSuites;
- model capability requirements;
- requested network, tool, filesystem, resource, token, cost, and secret
  reference permissions;
- health/readiness information, required evaluation gates, and build
  provenance.

Registry continues to store the original forward-compatible `spec` map. It
validates known v1 fields and round-trips unknown optional fields. Server-owned
identity, digest, signature, and verification evidence cannot be supplied by a
publisher. A mutable OCI tag is not sandbox-runnable; the image must contain an
`@sha256:` digest. MCP remains a tool protocol, not the agent invocation
protocol.

### Import snapshot

`POST /api/registry/imports` accepts a canonical pinned Registry reference and
an idempotency key. Identity supplies tenant, user, and ownership; the request
cannot. DevAI resolves the complete caller-visible dependency graph, verifies
the Registry digest/signature, validates the portable projection, and stores a
tenant-qualified immutable snapshot:

- canonical Registry ref, digest, signature key identity, and original agent;
- exact dependency refs and digests;
- runtime and permission projection;
- conformance findings and state;
- creation identity and timestamps.

The unique business key is tenant, project, and idempotency key. Repeating a
request returns the original import. Foreign tenant lookups return 404. The
snapshot is the authority for later runs, so Registry unavailability does not
invalidate an existing import.

### Conformance

Conformance is server-derived evidence:

1. `discoverable`: portable metadata validates;
2. `callable`: an authenticated remote A2A runtime is declared;
3. `sandbox_runnable`: an immutable OCI digest and reproducible dependency lock
   pass policy;
4. `verified`: the exact lock has passed required evaluations, supply-chain
   policy, and approval gates.

Findings carry a stable code, severity, field, and remediation. Publisher labels
cannot raise a level. Remote agents never receive `sandbox_runnable` because
DevAI cannot isolate their code.

### Sandbox and evaluation

`SandboxSpec` may reference an import ID. The service loads it within the
authenticated tenant scope and copies its exact agent version, digest,
dependency lock, runtime, and permissions into the stored sandbox spec. A
caller cannot override locked identity or widen permissions. Existing direct
sandbox creation remains compatible during migration.

Import verification that only reads Registry and commits one Postgres row stays
synchronous. OCI attestation, vulnerability checks, large dependency
verification, sandbox provisioning, multi-case evaluation, scoring, and cleanup
use Temporal because they cross more than two durable steps. Workflow IDs derive
from tenant, project, and request ID; activities remain idempotent and use
bounded retries with jitter. Valid terminal states are `ready`, `blocked`,
`failed`, `timed_out`, `cancelled`, and `stuck`.

### Ownership

- Agentic Registry owns catalog identity, validation, versions, signatures,
  visibility, and dependency resolution.
- DevAI owns import snapshots, projects, runs, sandboxes, traces, evaluations,
  and promotion evidence.
- OCI owns executable layers.
- OpenBao owns customer secret material. Contracts carry references only.
- Framework adapters live with the ADK or adapter package, not in the DevAI API
  process.

## Dependency failure behavior

- Registry down: exact existing imports and runs continue; new search/import
  fails explicitly.
- Semantic index delayed: exact-ref import continues and reports freshness.
- OCI or verifier down: import remains `pending_verification`; no floating image
  substitution occurs.
- Sandbox capacity unavailable: the durable run queues fairly per tenant.
- Agent readiness failure: the run fails with preserved logs and traces, then
  cleanup proceeds.
- Scoring unavailable: invocation evidence is retained and scoring retries; a
  real side-effecting tool is not replayed to recover a scorer.
- Optional telemetry unavailable: the run may continue with an observability
  warning because authoritative evidence is stored independently.

## Consistency, cost, and rollout

Import rows, locks, and audit intent commit in one Postgres transaction. Search,
analytics, and notifications may consume an outbox at least once and therefore
deduplicate by event ID. Postgres remains the source of truth; no new datastore
is introduced. Cost is bounded by existing per-tenant sandbox/model/judge quotas
and object-store retention. Import metadata is small enough for the existing
database at the stated scale.

Rollout is additive: contract validation, private imports, sandbox-runnable OCI,
remote callable agents, and framework adapters are independently enabled per
tenant. Rollback disables new imports or an adapter while retaining immutable
snapshots and evidence. Database cleanup is deferred until after the
compatibility window and requires a separately approved migration.

## Alternatives rejected

- Store only a Registry name and resolve `latest` at run time: not
  reproducible and vulnerable to dependency substitution.
- Copy executable images into Registry: creates a second OCI lifecycle,
  security, backup, and cost boundary.
- Treat A2A metadata or semantic similarity as execution authorization: mixes
  discovery with permission and fails closed nowhere.
- Load every framework SDK into DevAI: expands the control-plane supply chain
  and couples releases to third-party internals.
- Build another sandbox/evaluation service: duplicates delivered DevAI
  capabilities without a different data owner or scaling requirement.

## Consequences

Imports add a durable data model and verification state, but make evaluations
reproducible and allow Registry outages to degrade only new discovery. Framework
support becomes a contract-test problem rather than a growing set of branches
inside DevAI. A remote agent can be useful and evaluated while its weaker
isolation is represented honestly.
