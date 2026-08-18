# DevAI Settings Capability — Per-User / Per-Tenant Connectors & Secrets

Manage all of DevAI's connectors (LLM, SCM, memory, Slack, MCP, web-search) and their
credentials from one Settings page — scoped per **user**, **team**, **tenant**, or **global**.
Tenant user ownership is keyed by `tenant_id:subject_id`; tenant principals never
fall back to an email-only row. See [ADR-0001](adr/0001-tenant-qualified-integrations-and-metering.md).
Secret values are auto-provisioned into **GCP Secret Manager**; the app DB stores only references.
A user's own credentials transparently drive both their **conversations** and their **pipeline runs**.

---

## 1. How it works

```
   Settings UI ──POST /api/settings/connectors──▶ SettingsService ──┬─▶ Postgres user_settings
   (per scope)                                                      │     (prefs + secret REFS only)
                                                                    └─▶ SecretsAdapter ─▶ GCP Secret Manager
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
  to reject direct-provider connectors and require the `gateway` connector.
  Gateway calls receive sanitized tenant/user/run/agent attribution headers.
- **Cost isolation.** Redis usage projections and durable PostgreSQL LLM-call rows
  both carry tenant and user identity. Users see themselves, tenant admins see
  their tenant, and only `platform-admin` sees cross-tenant totals.

### Files

| File | Role |
|------|------|
| `src/devai/adapters/secrets/` | Secrets adapter family: `base.py` (ABC + `SecretRef`), `gcp_sm.py` (create/add-version via SDK), `env.py` (read-only), `noop.py`, `factory.py` |
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
| `DEVAI_SECRETS_PROVIDER` | `noop` | `noop` \| `env` \| `gcp_sm`. **`gcp_sm` is required to auto-provision secrets.** |
| `DEVAI_SECRETS_GCP_PROJECT` | `""` | GCP project for `gcp_sm` (falls back to `DEVAI_GKE_PROJECT`). |

With `secrets_provider=noop` the API still serves the catalog and non-secret prefs; secret writes
return a clear **409** and the UI shows a read-only banner.

---

## 3. Enabling secret auto-provisioning (GCP Secret Manager)

`gcp_sm` uses **Application Default Credentials** (Workload Identity in-cluster) — no key material in
code. It needs **write IAM**, which the devai service account does *not* have by default
(`app-secrets-devai-prod@tesseracthub-480811.iam.gserviceaccount.com` is read-only/`secretAccessor`).

Grant write access **out-of-band** (this is the one manual step; the code is inert until then):

```bash
gcloud projects add-iam-policy-binding tesseracthub-480811 \
  --member="serviceAccount:app-secrets-devai-prod@tesseracthub-480811.iam.gserviceaccount.com" \
  --role="roles/secretmanager.admin"      # or: secretCreator + secretVersionManager + secretAccessor
```

Then set on the devai deployment (via `tesserix-k8s`):

```
DEVAI_SECRETS_PROVIDER=gcp_sm
DEVAI_SECRETS_GCP_PROJECT=tesseracthub-480811
```

Provisioned secrets are named `devai-{scope}-{scope_id}-{connector}-{instance}-{field}` and labelled
`managed-by=devai` for easy auditing/cleanup.

> Until the IAM is granted, `can_write()` returns false, the UI shows "Secret storage is read-only",
> and secret writes 409 — non-secret preferences still work.

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
    secret_refs   JSONB NOT NULL DEFAULT '{}',   -- field -> GCP SM secret id (NEVER values)
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

`secrets{}` values are pushed to GCP SM and never echoed back. A secret write when the backend is
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
