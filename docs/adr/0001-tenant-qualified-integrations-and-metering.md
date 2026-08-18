# ADR-0001: Tenant-qualified integrations and metering

Status: accepted (application-side contract)

## Context and planning envelope

DevAI lets a principal bind LLM providers, SCM accounts, cloud/Kubernetes accounts, and MCP servers, then uses those bindings from chat and pipeline agents. Identity-provider subjects are unique only inside an issuer/tenant, so email or `uid` alone is not a safe ownership boundary.

Until production measurements replace these assumptions, size the control plane for 2,000 monthly active users, 200 concurrent users, 50 peak LLM requests/s, 20 peak MCP calls/s, 10 KiB or less of metadata per call, and roughly 2 million LLM calls/month. Settings are read-heavy (at least 100:1 reads:writes) and should remain below 100,000 connector rows over 36 months. The usage ledger stores aggregate counters plus a capped 300-call recent list per scope; durable per-call cost rows remain in PostgreSQL.

Target SLOs are 99.9% monthly availability, p99 below 300 ms for settings reads excluding an uncached Secret Manager fetch, and less than 50 ms p99 DevAI-added latency on an LLM gateway call. Provider latency is outside that gateway-overhead budget.

Assets worth protecting are provider credentials, infrastructure connections, prompts/completions, usage records, and cost attribution. Relevant actors are unauthenticated callers, an authenticated user in another tenant, an over-privileged tenant administrator, and a compromised downstream integration. The trust boundary is crossed at the auth BFF, Settings API, MCP Hub, LLM gateway, registry client, and analytics API; each boundary must derive identity server-side and authorize the concrete object or scope.

## Decision

Use the existing shared PostgreSQL schema and qualify every user-owned binding with `tenant_id:subject_id`. Tenantless local and service identities keep the legacy subject-only shape. Tenant principals never fall back to email-only lookup. Resolution remains most-specific-wins:

`user -> team -> org -> tenant -> global`

The registry owns reusable, non-secret definitions: agent specs, tools, MCP server catalog entries, model aliases, and policy metadata. Settings owns principal-specific bindings and Secret Manager references. Per-user secret values are never published to the registry. Runtime calls resolve the binding for the verified principal, then enter the MCP Hub or LLM adapter/gateway.

`DEVAI_LLM_GATEWAY_REQUIRED=true` is the fail-closed production control. In that mode the Settings API rejects direct Anthropic, OpenAI, Vertex, Groq, and OpenRouter connectors; only the gateway connector can be saved. Gateway requests carry sanitized `x-devai-tenant-id`, `x-devai-user-id`, `x-devai-run-id`, and `x-devai-agent` metadata. They do not carry prompt text, completion text, or provider secrets in those headers.

Metering writes exact micro-USD counters to tenant/user-qualified Redis namespaces for fast dashboards and writes every attributable call to PostgreSQL `agent_executions` with `tenant_id`, `user_id`, and `triggered_by` for durable reporting. Regular users read their own rows, tenant admins read their tenant aggregate, and only `platform-admin` reads the global aggregate.

Do not copy prompt or completion bodies into usage logs or LangSmith metadata. Logs and traces contain correlation identifiers, provider/model, token counts, latency, status, and estimated cost only.

## Consistency and failure behavior

- A settings row and its Secret Manager value cannot be committed atomically. The current operation writes the secret first and then the binding. A database failure can leave an orphan secret, but never a binding pointing at a value from another principal. Audit records contain secret reference names, not values.
- Usage writes are best-effort and must never fail an LLM call. PostgreSQL is the durable source for per-call reporting; Redis is a rebuildable dashboard projection.
- Gateway or provider timeout returns a bounded failure through the existing fallback chain. Strict gateway mode never falls back to direct internet egress.
- Registry failure degrades catalog discovery, not ownership checks. Existing principal bindings remain in Settings; unregistered personal credentials are never exposed to another tenant.
- MCP legs are cached by the tenant-qualified subject and expire after 120 seconds. A repeated call reconnects only that principal's legs.

## Migration and rollback

Apply `0004_tenant_usage_attribution.up.sql` before deploying code that expects scoped durable cost queries. The migration is additive and online: three text columns with empty defaults plus two indexes. The down migration drops the indexes and columns.

Existing tenant user-setting rows keyed only by `uid`/email are intentionally not read by tenant principals because their tenant cannot be proven from the row. Export those rows, map each owner from the authoritative IdP/team directory, and recreate them with `tenant_id:subject_id`. Do not guess tenant ownership from email domains. Tenantless local-development rows continue to work.

Rollback the application before applying the down migration. During a mixed-version rollout, new rows remain readable by old code only where callers use exact scope IDs; cost rows remain insertable because the new columns have defaults.

## Consequences and cost

This avoids per-tenant databases, connection-pool multiplication, and cross-store joins in the hot path. At the planning envelope, settings storage is tens of megabytes. Durable usage is approximately 2 million narrow PostgreSQL rows/month; at roughly 0.5-1.0 KiB including indexes, budget 1-2 GiB/month before retention/compression. Redis growth is driven by tenant/user/model/day aggregates rather than call count; recent lists are capped.

The trade-off is an explicit migration for old unqualified rows and a hard operational dependency on gateway model aliases/credentials when strict mode is enabled. A gateway connector must be validated in staging before flipping the flag.

## Alternatives rejected

- Per-tenant databases: rejected because the current scale and compliance requirements do not justify migration, backup, and pool overhead.
- Email-only keys: rejected because emails and IdP subjects are not globally tenant-unique.
- Publishing user secrets or secret references into the agentic registry: rejected because the registry is a catalog/control plane, not a principal credential vault.
- Silent direct-provider fallback from strict gateway mode: rejected because it bypasses policy, telemetry, and egress controls precisely when the gateway is unavailable.

## Not included

Dynamic creation of external agentgateway routes for arbitrary personal MCP endpoints is not implemented in this repository because no authenticated route-provisioning contract exists here. Personal MCP definitions remain Settings bindings and execute through the identity-aware MCP Hub. If production requires every personal MCP network hop to traverse Solo agentgateway, the gateway/registry deployment must first expose a tenant-scoped, idempotent route-binding API; DevAI should then reconcile bindings with an outbox rather than perform an unsafe dual write.
