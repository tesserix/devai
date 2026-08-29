# ADR 0004: Run DevAI specializations on Tesserix ADK in process

- Status: accepted
- Date: 2026-08-21

## Context

DevAI has 40 YAML specializations used by ALM, review, orchestration, and SRE
workflows. They already execute inside DevAI with request-scoped access to user LLM
connectors, SCM identities, tenant settings, tool allowlists, dry-run controls, audit
attribution, and sandbox gateways. Tesserix ADK v0.51.0 adds a typed agent definition,
bounded run loop, structured output validation, tool-call enforcement, usage reporting,
and trace events that these agents should share.

The design envelope is 20 new agent invocations per second at peak, at most 32 text
parts and 256 KiB per A2A message, and 100 concurrent model/tool runs per API replica.
Catalog reads are expected to outnumber publishes by at least 10:1. The change creates
no new durable data: run, audit, and usage records remain in the existing stores, so
12- and 36-month storage growth is unchanged. The control-plane target is 99.9%
monthly availability and p99 below 500 ms for authentication, validation, and dispatch;
agent completion remains bounded by each specialization's declared timeout and its
external model/tool latency.

Assets worth protecting are users' LLM and SCM credentials, tenant run data, cluster
authority, and tool results. Threat actors include an unauthenticated caller, an
authenticated user from another tenant, prompt or tool-result injection, and a
compromised dependency. Trust boundaries are the authenticated request into DevAI, the
model output entering the tool dispatcher, and DevAI's outbound provider/tool calls.
Every boundary validates or narrows authority. Secret values stay in the settings and
Secret Manager resolution path and never enter registry manifests or ADK definitions.

## Decision

DevAI runs each non-legacy specialization through Tesserix ADK inside the existing
`SpecAgent` execution path.

- `runtime: tesserix_adk` is explicit in all 40 specialization files. `auto` preserves
  the native runner for external and in-memory specs that predate the runtime field;
  `legacy` remains the explicit Python-class rollback value.
- `AgentDispatcher` resolves the full principal once, then supplies that principal's
  dynamic LLM chain, SCM client, and settings overlay to the ADK bridge.
- The ADK provider is an adapter over DevAI's user-authorized provider chain. It does
  not read a global Vertex, OpenAI, Anthropic, or Gemini key directly.
- ADK tool declarations come only from each specialization's `allowed_tools`. Calls
  still execute through DevAI's central `ToolDispatcher`, including dry-run blocking,
  tenant-aware clients, sandbox gateway modes, and audit context.
- Structured handovers are generated from each specialization's existing schema.
  Usage and ADK trace events are mapped back to the existing `AgentRunResult` contract.
- DevAI API and runner images use the verified multi-architecture base image
  `ghcr.io/tesserix/base-python-adk-3.13:20260820` at digest
  `sha256:cca20646be7d01045fe3fa4c411cdaff8df600da7a3d7769b9786b5282d18f9a`.
  That image contains the attested ADK v0.51.0 wheel. An unavailable kit is a build
  failure, never a runtime fallback.
- The authenticated endpoint `POST /a2a/v1/{agent_name}` exposes the same catalog. It
  accepts only strict JSON-RPC `message/send` text messages, derives identity from the
  verified auth-BFF principal, uses the live pipeline dependency bundle, and returns a
  generic error without provider details.
- Registry Agent manifests update the existing `*-agent` records in place, advertise
  the DevAI A2A endpoint, and declare `provider: devai-user-routing`, `model: dynamic`,
  and `ai.tesserix.dev/provider-policy: user-connectors`. Provider selection is runtime
  policy, not catalog data.
- Kagent is the preferred sandbox execution backend where the sandbox capability is
  enabled. This is separate from the agent runtime: Tesserix ADK defines and runs the
  agent, while the sandbox backend decides where isolated work executes.

High- and critical-risk specializations retain their existing workflow approval state;
direct A2A invocation returns `409` before model or tool execution. This change does not
turn an A2A endpoint into an approval API. Mutating authority stays bounded by the
specialization tool allowlist and the authenticated principal. Workflows that require a
human gate continue through the pipeline approval endpoints.

## Failure behavior

- Missing or invalid principal: `401`; no provider or tool resolution.
- Invalid or oversized A2A input: `422`; no agent execution.
- Unknown agent: `404` with no catalog disclosure.
- High- or critical-risk direct invocation: `409`; callers must use the workflow
  approval path.
- Pipeline dependency bundle unavailable: `503`; the route does not construct a
  platform-credential fallback.
- All user-authorized providers unavailable: the resolved provider chain exhausts its
  ordered same-provider and cross-provider fallbacks, then the run fails without
  borrowing an unauthorized platform credential.
- Model, tool, or ADK failure: a stable generic HTTP error; detailed diagnostics stay
  in tenant-attributed logs and traces.
- Duplicate Temporal workflow starts use the business workflow ID and are rejected by
  the existing durable workflow adapter. A2A `message/send` does not create a durable
  workflow record and callers must not use it as a replacement for a durable pipeline
  command.
- A process crash during a model or tool call leaves the same uncertainty as the
  underlying call. Idempotent workflow activities may retry; non-idempotent tool effects
  remain indeterminate and are not blindly replayed.

## Alternatives considered

- Run equivalent agents in a separate Kora/`ai-agents` service: rejected because user
  provider secrets, SCM credentials, and tool authority would cross another service
  boundary and duplicate DevAI's tenancy and audit controls.
- Keep the native in-tree loop as the shipped-catalog default: rejected because it
  would preserve two execution semantics for registry agents and make their runtime
  declarations misleading. It remains only for compatibility specs with `runtime: auto`.
- Hard-code Vertex or another provider into each Agent manifest: rejected because it
  breaks user authorization, provider failover, and future providers.
- Make Kagent the agent definition/runtime: rejected because Kagent is an execution
  backend for sandboxed workloads, not the provider-routing or typed-agent contract.

## Consequences

There is no new service, database, load balancer, or cross-service secret transfer. The
incremental infrastructure cost is the larger shared ADK base image and existing model
and tool usage. The image layer is shared across DevAI agent workloads, reducing repeated
dependency installation and build time.

Migration is an in-place registry update plus an application image rollout. Rollback is
one GitOps image-digest revert; the `legacy` runtime remains available for a specific
specialization if an ADK incompatibility is isolated. No schema migration or data
contraction is required.
