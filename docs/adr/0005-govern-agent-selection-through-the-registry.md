# ADR 0005: Govern agent selection through Agentic Registry

- Status: accepted
- Date: 2026-08-23
- Supersedes: the direct catalog execution behavior in ADR 0004

## Context

DevAI has 40 reviewed product capabilities and their Tesserix ADK runtimes. Agentic
Registry stores the deployable Agent, Skill, Prompt, Tool, and MCP Server artifacts,
while Solo AI Gateway and agentgateway are the approved model and MCP data planes.
Previously, DevAI merged arbitrary published Registry agents into its local catalog,
fell back to local YAML when Registry was unavailable, and allowed named A2A requests
to execute without resolving the Registry composition. Publication therefore acted as
execution admission and dependency failure could bypass governance.

The design envelope remains 20 invocations per second at peak, at most 32 text parts
and 256 KiB per A2A message, and 100 concurrent model/tool runs per API replica. The
control-plane target is 99.9% monthly availability and p99 below 500 ms for
authentication, Registry resolution, admission, and dispatch, excluding agent
completion. Registry reads dominate writes by at least 10:1. This change adds no
durable data, so 12- and 36-month storage growth is unchanged.

Assets are user LLM and SCM credentials, tenant data, tool authority, model spend, and
cluster access. Threat actors include unauthenticated callers, another tenant's user,
a publisher of a malicious Registry artifact, a compromised Registry or gateway, and
prompt/tool-result injection. Trust boundaries are the authenticated DevAI request,
Registry responses, model output, and outbound model/tool calls. Each boundary is
validated and defaults to denial.

## Decision

DevAI separates product admission from deployed composition:

- Local specialization YAML is the reviewed admission list and runtime contract. A
  Registry publish cannot add a runnable product capability.
- A capability maps deterministically to one canonical Agent:
  `requirements_analyst` maps to `requirements-analyst-agent`. DevAI resolves that
  exact object with `GET /v0/agents/{name}/resolved`; it never searches for a loosely
  matching card or executes a Registry-provided URL.
- The resolution must contain no unresolved references. The Agent must declare the
  reviewed runtime, provider policy, dynamic user routing, version, Skill, and Prompt.
  Resolved Skill tools, context keys, output key, handover schema, and risk level must
  equal the reviewed local contract. Resolved Prompt content and hash must also equal
  it. Extra direct Tool authority is denied.
- Generated Agent manifests reference the generated Skill and Prompt artifacts by
  name. Prompts retain `promptRef` for compatible Registry export adapters and also
  appear in `prompts` so `/resolved` includes them.
- Both `POST /a2a/v1/capabilities/{capability}` and the compatible named route
  `POST /a2a/v1/{agent_name}` use the same governed admission path. Successful results
  include a safe composition snapshot containing artifact names and version, never
  prompt bodies, credentials, or upstream error content.
- Governed execution requires `DEVAI_LLM_GATEWAY_REQUIRED=true` and a non-empty
  `DEVAI_LLM_GATEWAY_BASE_URL`. A bundle declaring MCP Servers additionally requires
  `DEVAI_AGENTGATEWAY_URL`. Agent, Registry, A2A, model, or tool failure never retries
  through a direct provider or Agent Card URL.
- High- and critical-risk capabilities still return `409` from direct A2A execution
  and must use the existing workflow approval path.

Registry composition is strongly consistent for each invocation: DevAI performs a
fresh `/resolved` read and executes only the validated snapshot. Registry is a critical
dependency for governed execution, not a degradable cache. Local YAML remains usable
for catalog inspection and as the reviewed side of validation, but never substitutes
for a missing Registry resolution.

## Failure behavior

- Unknown or unreviewed capability: `404` without catalog disclosure.
- Registry unavailable, malformed response, wrong Agent, policy mismatch, unresolved
  reference, or Skill/Prompt/Tool drift: `503`; no ADK, model, or tool call.
- Mandatory LLM gateway policy disabled or URL missing: `503` before Registry or model
  access.
- Resolved MCP Servers with no MCP gateway: `503` before agent execution.
- High- or critical-risk direct invocation: `409` before model or tool access.
- ADK, model, or tool failure after admission: stable generic error. Detailed internal
  diagnostics remain in attributed logs and traces; Registry response bodies are not
  copied into client errors or logs.
- A timeout or crash after a non-idempotent tool is accepted remains indeterminate and
  is not replayed blindly. Durable workflow activity retry and idempotency rules from
  ADR 0004 still apply.

## Alternatives considered

- Continue merging published agents into the runtime catalog: rejected because
  publication is not product authorization and a rogue duplicate can gain reachability.
- Fall back to local YAML during a Registry outage: rejected because it executes an
  unverified composition and makes removal or revocation ineffective.
- Discover by capability across all Agent Cards and call the selected URL directly:
  rejected because ambiguous matching and ungoverned URLs bypass reviewed mappings and
  DevAI's gateway, identity, approval, and tool controls.
- Cache resolved bundles through Registry outages: rejected for the initial design
  because revocation semantics and maximum staleness are not yet defined. A future
  signed, bounded-staleness cache requires a separate ADR.

## Consequences

Registry availability now directly affects governed agent execution, which is the
intended fail-closed tradeoff. At 20 peak invocations per second, one fresh Registry
read per invocation remains within the existing control-plane envelope and adds no new
service or datastore. Model and tool traffic continues through existing gateways, so
incremental infrastructure cost is negligible beyond Registry read load and telemetry.

Migration consists of publishing regenerated Agent manifests, enabling mandatory LLM
gateway policy in GitOps, and rolling out the DevAI image. Rollback is one image and
manifest revert, but rollback re-enables the documented admission weakness and should
be used only for service restoration. No database migration is required.
