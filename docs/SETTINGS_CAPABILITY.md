# DevAI Settings Capability — Per-User / Per-Tenant Connectors & Secrets

Manage all of DevAI's connectors (LLM, SCM, memory, Slack, MCP, web-search) and their
credentials from one Settings page — scoped per **user**, **team**, **tenant**, or **global**.
Tenant user ownership is keyed by `tenant_id:subject_id`; tenant principals never
fall back to an email-only row. See [ADR-0001](adr/0001-tenant-qualified-integrations-and-metering.md).
Secret values are auto-provisioned into **OpenBao**; the app DB stores only references.
A user's own credentials transparently drive both their **conversations** and their **pipeline runs**.

---

## 1. How it works

```
   Settings UI ──POST /api/settings/connectors──▶ SettingsService ──┬─▶ Postgres user_settings
   (per scope)                                                      │     (prefs + secret REFS only)
                                                                    └─▶ SecretsAdapter ─▶ secret-service ─▶ OpenBao
                                                                          (secret VALUES only)

   A request/run for user U ─▶ build_overlay(U) ─▶ PrincipalSettingsOverlay ─▶ existing adapter factories
        resolution order:  user → team(s) → tenant → global         (getattr(settings, …) — no factory change)
```

- **Secret split.** Values never touch Postgres. `SettingsService` writes them to the secrets
  backend and persists only the returned `SecretRef` name. Reads only ever report *which* fields
  have a secret set.
- **The overlay** wraps the global `Settings` and overrides just the attributes a Principal
  configured. Because every adapter factory reads config via `getattr(settings, …)`, handing them
  the overlay routes a user's own creds into the same factories — zero duplication.
- **Scope resolution** is most-specific-wins: `user → team → tenant → global`.
- **Gateway enforcement.** Set `DEVAI_LLM_GATEWAY_REQUIRED=true` in production
  to route every provider connector through its native AgentGateway route.
  Gateway calls receive sanitized tenant/user/run/agent/provider attribution headers.
- **Cost isolation.** Redis usage projections and durable PostgreSQL LLM-call rows
  both carry tenant and user identity. Users see themselves, tenant admins see
  their tenant, and only `platform-admin` sees cross-tenant totals.

### Files

| File | Role |
|------|------|
| `src/devai/adapters/secrets/` | Secrets adapter family: `base.py` (ABC + `SecretRef`), `openbao.py` (read-only OpenBao reads and brokered blind writes), `gcp_sm.py`, `env.py`, `noop.py`, `factory.py` |
| `src/devai/settings/models.py` | `CONNECTOR_SPECS` catalog (the connectors + fields + which map to which `Settings` attr) + `Connector`/`Scope` |
| `src/devai/settings/service.py` | `SettingsService` — Postgres `user_settings` store (in-memory fallback) + secret provisioning |
| `src/devai/settings/overlay.py` | `PrincipalSettingsOverlay` + `build_overlay()` — the per-user config facade |
| `src/devai/settings/routes.py` | `GET/POST/DELETE /api/settings/*` — Principal-gated, scope-authorized |
| `src/devai/chat/gateway.py` | Resolves a per-Principal overlay → per-user chat agent (cached by fingerprint) |
| `src/devai/pipeline/interfaces.py` + `bootstrap.py` | `StageDeps.secrets` + `StageDeps.settings_service` so runs can resolve the overlay |
| `dashboard/src/app/settings/page.tsx` | The Settings UI (connectors, scope selector, masked secret fields) |

---

## 2. Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `DEVAI_SETTINGS_ENABLED` | `true` | Enable the Settings capability + API. |
| `DEVAI_SECRETS_PROVIDER` | `noop` | `noop` \| `env` \| `gcp_sm` \| `openbao`. **`openbao` is the production backend.** |
| `DEVAI_SECRETS_OPENBAO_ADDR` | `""` | OpenBao service URL. |
| `DEVAI_SECRETS_OPENBAO_ROLE` | `read-devai-api` | Read-only Kubernetes auth role. |
| `DEVAI_SECRETS_BROKER_URL` | `""` | Internal secret-service URL used for blind writes and soft deletes. |

With `secrets_provider=noop` the API still serves the catalog and non-secret prefs; secret writes
return a clear **409** and the UI shows a read-only banner.

---

## 3. Enabling secret auto-provisioning (OpenBao)

DevAI has no OpenBao write policy. It reads under the `read-devai-api` Kubernetes role and sends
writes/deletes to secret-service with a projected service-account token whose audience is
`secret-service`. The broker verifies the token through Kubernetes TokenReview and fixes the path
prefix server-side.

Set on the DevAI deployment through GitOps:

```
DEVAI_SECRETS_PROVIDER=openbao
DEVAI_SECRETS_OPENBAO_ADDR=http://openbao.openbao.svc.cluster.local:8200
DEVAI_SECRETS_OPENBAO_ROLE=read-devai-api
DEVAI_SECRETS_BROKER_URL=http://secret-service-api.secret-service.svc.cluster.local:8080
```

Provisioned secrets live at `devai/devai-api/<owner-hash>/<sanitized-name>`. The owner hash is derived
from the tenant-qualified scope. Secret values never appear in broker audit records.

> If the broker, TokenReview, or OpenBao policy is unavailable, `can_write()` returns false, the UI
> shows "Secret storage is read-only", and secret writes return 409. Non-secret preferences still work.

---

## 4. Database schema (lives in tesserix-k8s)

Per the project rule, the schema goes in
`tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/devai/devai_db/user_settings.sql`
(the bootstrap CronJob applies it idempotently). Reference DDL:

```sql
CREATE TABLE IF NOT EXISTS user_settings (
    scope         TEXT NOT NULL,          -- user | team | tenant | global
    scope_id      TEXT NOT NULL DEFAULT '',
    connector_key TEXT NOT NULL,          -- llm | scm | memory | slack | mcp | web_search
    instance_id   TEXT NOT NULL DEFAULT 'default',
    provider      TEXT NOT NULL DEFAULT '',
    prefs         JSONB NOT NULL DEFAULT '{}',   -- non-secret field values
    secret_refs   JSONB NOT NULL DEFAULT '{}',   -- field -> OpenBao secret ref (NEVER values)
    enabled       BOOLEAN NOT NULL DEFAULT true,
    updated_by    TEXT NOT NULL DEFAULT '',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, scope_id, connector_key, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_user_settings_scope ON user_settings (scope, scope_id);
```

If the table is absent, `SettingsService` degrades to an in-memory store (dev/degraded) and logs it.

---

## 5. API

All endpoints require an authenticated Principal (`extract_principal` → 401). Authorization:
a user may manage their own `user` scope and their `team` scopes; `tenant`/`global` require the
`admin` role.

| Method & path | Purpose |
|---------------|---------|
| `GET /api/settings/catalog` | The connector catalog (fields, which are secret) + `secrets_writable` flag |
| `GET /api/settings` | The caller's visible connectors (user + teams + tenant + global). Never returns secret values |
| `POST /api/settings/connectors` | Create/update a connector. Body: `{scope, scope_id?, connector_key, provider, instance_id?, prefs{}, secrets{}}` |
| `DELETE /api/settings/connectors/{scope}/{scope_id}/{connector_key}?instance_id=` | Remove a connector + its provisioned secrets (`-` means the global empty scope_id) |

`secrets{}` values are pushed to OpenBao through the blind-write broker and never echoed back. A secret write when the backend is
read-only returns **409**.

---

## 6. Connectors shipped

| Connector | Provider choices | Secret fields | Drives |
|-----------|------------------|---------------|--------|
| **LLM** | anthropic / openai / noop | anthropic_api_key, openai_api_key | chat + agents |
| **Source Control** | github / gitlab / azure_devops | scm_token | repo reads, PRs |
| **Agent Memory** | redis / pgvector / mem0 / zep / hondo / noop | mem0_api_key, zep_api_key | cross-run memory |
| **Slack** | on / off | slack_bot_token, slack_signing_secret | the Slack channel |
| **MCP Server** (multi) | streamable_http / sse | mcp_token | per-user external MCP tools |
| **Web Search** | noop / tavily | tavily_api_key | agent web-search tool |

The catalog is data-driven (`CONNECTOR_SPECS`) — adding a connector is one entry + (if new) a
`Settings` attribute; the UI and overlay pick it up with no further code.

---

## 7. Tests

- `tests/unit/test_secrets_adapters.py` — factory degradation, noop refuses writes, env read-only, id sanitization, `SecretRef` roundtrip.
- `tests/unit/test_settings_service.py` — secret-ref-not-value storage, scope resolution (user wins), MCP collection, delete reverts to global.
- `tests/unit/test_settings_routes.py` — auth (401), authorization (user/team/tenant/global), secret-readonly 409, unknown-connector 400.

---

## 8. Security properties

- Secret **values** live only in the secrets backend (GCP SM) — never in Postgres, never returned by
  the API, never logged (the `Connector.public_dict()` view elides them).
- Every settings endpoint is Principal-gated; scope writes are authorized against the caller's
  uid / team membership / admin role.
- The secrets backend **never silently no-ops a write** — a missing-IAM/disabled backend surfaces a
  409 to the UI rather than pretending to store a value.
- The overlay is read-only and per-request; resolved secret values are held only for the lifetime of
  the turn/run, never persisted into `agent_context` or task state.
