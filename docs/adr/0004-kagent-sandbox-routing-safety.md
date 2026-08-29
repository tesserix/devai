# ADR 0004: Gate kagent-preferred sandbox routing on parity and acceptance

## Context

DevAI should eventually prefer kagent for sandbox work, with the supported Job
runtime as fallback. Today a DevAI sandbox pins agent, model, prompt, dataset,
runtime, tool modes, budgets, credentials, workspace, network grants, and TTL.
The registry export creates a standing kagent `SandboxAgent` from a published
Agent, static ModelConfig, prompt, and MCP references. It does not carry the full
per-sandbox contract.

Production evidence is also incomplete. The canary is not Ready, the WorkerPool
mounts a Kubernetes token and remains privileged, dedicated-node placement and
least-privilege egress are unproven, and 5/20/50 Actor tests have not produced
failure, queue-depth, resource, p50/p95 cold-start, or run-latency measurements.
There is therefore no defensible Actor availability or latency SLO yet.

## Decision

Keep `DEVAI_KAGENT_ENABLED=false` as the platform default and preserve the
existing `sandbox is not None` dispatch guard. The registry label
`devai.io/runtime=kagent` continues to opt an ordinary published Agent into the
kagent reconciler; it does not claim sandbox feature parity.

Before kagent can become the sandbox default, implement a sandbox-scoped Actor
lifecycle from the immutable `SandboxSpec`: create, wait for Accepted and Ready,
dispatch with a deterministic sandbox/run idempotency key, record attribution and
budgets, then tear down at TTL. Unsupported fields must fail closed rather than be
silently dropped.

Fallback is allowed only while DevAI knows kagent did not accept the request. A
connect failure can select a Job or another model variant. A timeout, 5xx, invalid
2xx response, JSON-RPC error, or returned failed task may follow partial execution,
so it produces a typed uncertain outcome and is never replayed automatically.

## Alternatives considered

- Default every sandbox to the existing standing `SandboxAgent`: rejected because
  it silently loses immutable pins, tool modes, budgets, workspace, network grants,
  and TTL semantics.
- Fall back after every kagent error: rejected because response loss after
  acceptance can execute side-effecting tools twice.
- Remove kagent from sandbox planning: rejected because shared Actors may reduce
  cold start and footprint once parity, isolation, and capacity are proven.

## Consequences

Ordinary labelled agents can still use kagent when the scoped switch is enabled.
Definite pre-acceptance failures retain Job availability; ambiguous failures are
visible and require reconciliation using the stable A2A identifiers. Production
rollout remains a GitOps change with one-step rollback to the default-off flag.

The next decision requires published compatible kagent/Substrate artifacts,
tokenless WorkerPools, reviewed privilege and node placement, passing network and
cross-tenant tests, full sandbox capability handling, and measured 5/20/50 results.
Until those numbers exist, cost and latency improvement remain hypotheses rather
than rollout justification.
