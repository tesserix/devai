# ADR 0008: Cluster-global ADK runtime

## Status

Accepted.

## Context

Tesserix needs one stable runtime entry point that agents in admitted product
namespaces can call without coupling to a DevAI service name. The first capacity
envelope is 20 new runs per second, 100 concurrent interactive runs, and 10
parallel evaluation Jobs. The service targets 99.9% monthly availability. Agent
Registry discovery remains a critical dependency for new governed invocations
and targets p99 below 300 ms.

The protected assets are agent prompts and composition, model and tool authority,
workload identity, tenant context, run evidence, and the ability to invoke an
approved agent. Threats include an unauthenticated workload, a compromised
machine client, a caller forging identity headers, an unreviewed Registry object,
and one product exhausting shared capacity. Trust crosses the calling workload,
Zitadel, AgentGateway, the runtime host, Agent Registry, model gateway, and MCP
gateway. Every crossing validates identity and the exact admitted object.

## Decision

The cluster-internal consumer contract is:

```text
http://agentgateway-mcp.agentgateway-system.svc.cluster.local:8082/a2a/v1/...
```

AgentGateway owns caller authentication and coarse workload authorization. It
verifies the Zitadel signature, issuer, audience, expiry, and
`agentgateway.runtime` role. It overwrites workload subject and client-id headers,
then replaces the caller token with a dedicated upstream bearer. The runtime
compares that bearer in constant time before trusting either header. The bearer
is accepted only on A2A and Agent Card routes; it does not broaden the rest of
the DevAI API.

The backend implementation remains owned by the DevAI control plane, but callers
use the stable `adk-runtime.devai.svc.cluster.local` alias and never the
`devai-api` service name. "Global" means cluster-shared and product-neutral at
the consumer boundary. It does not mean public, anonymous, or that publication
to Agent Registry alone grants execution authority.

The warm runtime pool runs at three replicas and may scale to nine. Replicas are
spread across three zones and distinct hosts, retain at least two instances
during voluntary disruption, drain for the longest admitted request, and relay
live events through Redis Pub/Sub. Durable run state and logs remain external to
the pod. The existing AgentGateway data plane runs two replicas across zones with
a disruption budget. Each gateway replica admits 1,800 requests per minute with
a burst of 200, so one surviving replica can absorb the 20 starts/s target with
50% headroom. The HA global limiter also enforces 1,800 requests per minute per
verified Zitadel subject and fails closed if its rate-limit service is unavailable.

Interactive, reviewed YAML specializations may execute in the warm pool.
Long-running background work, scheduled work, and evaluations continue to use
Jobs or Temporal-orchestrated sandbox Jobs. Generated code never executes in the
warm trusted process. A new hosted capability needs both a reviewed local runtime
admission specification and an exact Registry composition that passed its
evaluation gate. A new caller needs its own Zitadel machine client, role grant,
and explicit namespace/network admission.

## Failure behavior

- AgentGateway unavailable: callers fail; there is no direct-runtime bypass.
- Zitadel or JWKS unavailable: cached keys may serve until expiry, then requests
  fail closed.
- Runtime replica or node unavailable: the Service routes to remaining ready
  replicas and the disruption budget retains two during voluntary maintenance.
- Redis unavailable at startup: a new replica does not become a partial HA
  member. If a running relay later fails, durable run evidence remains available
  but cross-replica live fan-out is degraded and logged.
- Registry unavailable: new governed resolution fails with a generic 503; no
  floating or locally guessed composition is executed.
- Upstream bearer missing or mismatched: the runtime returns 401 and ignores
  workload identity headers.
- One consumer exceeds its allowance: the global limiter isolates its verified
  subject; the local limiter retains an aggregate per-proxy safety ceiling.

## Rollout and rollback

Rollout is additive and ordered:

1. Release the ADK 0.53.1 runtime image while the global route is absent.
2. Create one random upstream bearer in GCP Secret Manager and reconcile it into
   both `devai` and `agentgateway-system`.
3. Scale the runtime to three replicas and verify readiness, zone spread, event
   relay, and disruption protection.
4. Reconcile the AgentGateway backend, strict policy, role, and internal route.
5. Admit one caller namespace and machine client, run Agent Card and A2A probes,
   then onboard additional consumers independently.

Rollback first removes or disables the global route so an older application
image is never exposed with incompatible authentication. Reverting the GitOps
release restores the prior image and replica policy in one reconciliation. The
credential may remain unused for a later retry; rotating it is reserved for a
credential incident. There is no schema migration or irreversible data change.

## Cost

The previous minimum was one pod requesting 384 MiB. Three replicas reserve
1,152 MiB, an incremental minimum of 768 MiB; at the observed approximately
361 MiB working set, the two added replicas consume about 722 MiB before sidecar
overhead. Nine replicas reserve 3,456 MiB, an increase of 3,072 MiB over the old
ceiling. No new datastore or always-on gateway is introduced. Evaluation and
background Jobs remain bounded variable cost.

## Alternatives rejected

- A new standalone runtime service: duplicates Registry, model, MCP, audit, and
  lifecycle wiring already owned by DevAI without creating a distinct data owner.
- One replica with durable storage: preserves records but not availability or
  live interactive behavior during a pod loss.
- Direct access to the backend Service: lets callers forge identity headers and
  bypass role, rate-limit, and audit policy.
- Automatic execution of every Registry Agent: makes publication equivalent to
  runtime authorization and permits unreviewed prompts, tools, or runtimes into a
  shared trusted process.
- Namespace wildcard admission: turns one compromised workload into a
  cluster-wide runtime credential.
