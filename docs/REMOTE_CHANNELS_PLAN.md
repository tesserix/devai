# DevAI Remote Channels — Slack + Remote URL + MCP (Adapter-based)

**Goal:** Talk to DevAI from anywhere — Slack, a remote app/URL thread, or any MCP client — all
routed through **one** conversational core, built as a clean adapter family (CLAUDE.md §6), wired
end-to-end through the existing DB / Redis / NATS / A2A / Principal plumbing. No duplicated logic.

---

## The one idea

Today the chat brain (`src/devai/chat/agent.py`, 19 tools, sessions, ReAct loop) is reachable only
via the dashboard's REST/SSE/WebSocket routes (`src/devai/chat/routes.py`). We do **not** duplicate
it. We put a thin **transport-agnostic gateway** in front of it, and make every channel a thin
transport over that gateway:

```
                      ┌─────────────────────────────────────────────┐
   Slack  ───────────▶│                                             │
   (Events API)       │            ConversationGateway              │
                      │   handle_turn(ConversationTurn)             │
   Remote URL ───────▶│      → chat agent (existing 19 tools)       │──▶ Redis sessions
   (/threads/*)       │      → A2A inject / pipeline trigger        │──▶ NATS events
                      │      → Principal attribution + audit        │──▶ Postgres audit
   MCP client ───────▶│                                             │──▶ A2A bus
   (/mcp streamable)  │   ONE brain — written once, never copied    │
                      └─────────────────────────────────────────────┘
```

- **ConversationTurn** (in) / **ConversationReply** (out) — transport-neutral DTOs.
- **`conversation_id`** is the universal thread key: `slack:{channel}:{thread_ts}`,
  `url:{thread_id}`, `mcp:{session_id}`. Same key → same Redis-backed history → continuity across
  channels.
- Each transport is a `MessagingChannel` subclass whose `dispatch()` is a **never-raise** boundary
  (parse → gateway → deliver; any error logs + degrades to noop). Matches the adapter contract.

---

## Adapter family layout (per CLAUDE.md §6)

New family `src/devai/adapters/messaging/` (the inbound-conversation transport family):

```
src/devai/adapters/messaging/
  __init__.py        re-exports public surface
  base.py            MessagingChannel ABC + ConversationTurn/ConversationReply + the gateway protocol
  factory.py         create_messaging_channels(settings, gateway) — reads DEVAI_MESSAGING_* flags
  noop.py            NoopChannel (mandatory) — used in tests + disabled mode
  slack.py           SlackChannel  (lazy import slack_sdk/slack_bolt)
  remote_url.py      RemoteUrlChannel (plain HTTP thread transport, no extra SDK)
  mcp.py             McpChannel (the MCP tool surface; lazy import `mcp`)
```

Core (transport-independent), lives next to the chat agent so it owns the brain:

```
src/devai/chat/gateway.py    ConversationGateway — wraps the existing ChatAgent, owns memory + A2A
```

Settings (one provider-style flag per channel + creds), in `config.py` under a
`# --- messaging adapter ---` block:

```
DEVAI_SLACK_ENABLED=false           slack_enabled: bool
DEVAI_SLACK_BOT_TOKEN=              slack_bot_token: str         (→ GCP Secret Manager in prod)
DEVAI_SLACK_SIGNING_SECRET=         slack_signing_secret: str    (→ GCP Secret Manager in prod)
DEVAI_SLACK_ALLOWED_CHANNELS=       slack_allowed_channels: str  (csv; empty = all)
DEVAI_REMOTE_CHAT_ENABLED=true      remote_chat_enabled: bool
DEVAI_MCP_SERVER_ENABLED=true       mcp_server_enabled: bool
DEVAI_MCP_REQUIRE_AUTH=true         mcp_require_auth: bool        (Keycloak JWT bearer on /mcp)
```

All secrets follow the existing rule: **GCP Secret Manager + ExternalSecret**, never in the DB or git.

---

## Transports (thin)

### 1. Slack — `POST /webhook/slack` (HTTP Events API)
- We already have a public Istio ingress, so use the **HTTP Events API** (stateless, scales behind
  the Service; no multi-pod Socket-Mode fan-out). Socket Mode stays a local-dev option.
- Verify every request: `X-Slack-Signature` v0 = HMAC-SHA256 over `v0:{ts}:{raw_body}`,
  `hmac.compare_digest`, 5-min replay window — hashing the **raw body bytes** (mirrors the webhook
  hardening in Phase 0).
- Handle the `url_verification` challenge handshake.
- **Ack within 3 s, work async:** return 200 immediately, run the turn in the background, post the
  answer back with `chat.postMessage(thread_ts=…)` so replies stay in-thread. De-dup on Slack
  `event_id` (Redis `SETNX`, short TTL) so retries don't double-run the pipeline.
- Principal: synthesize from the Slack user (`Principal.slack(team, user_id, email)` — a small
  addition mirroring `Principal.webhook(...)`).
- Scopes: `app_mention`, `chat:write` (+ `message.im`/`im:history` for DMs).

### 2. Remote URL — `POST /threads/{thread_id}/messages` + `GET /threads/{thread_id}/stream` (SSE)
- A plain, embeddable HTTP thread API for "its own remote URL thread" — any remote app can POST a
  message and read replies (sync body) or subscribe to the SSE stream for token-by-token output
  (reuses the existing chat SSE pattern).
- Auth: Keycloak bearer (same JWT middleware as MCP) or the existing `devai_session` cookie via
  `extract_principal` — so it works both embedded-in-dashboard and as a standalone remote URL.

### 3. MCP — `mount("/mcp")` (FastMCP, Streamable HTTP)
- Expose DevAI **as an MCP server** so any MCP-capable client/agent can reach the same brain.
  Tools: `chat(message, conversation_id?)`, `trigger_pipeline(repo, requirements)`,
  `pipeline_status(run_id)`, `inject_requirements(run_id, message)` — each is a thin wrapper over the
  gateway / existing pipeline service.
- Transport: **Streamable HTTP** on a single `/mcp` endpoint, mounted as an ASGI sub-app into the
  existing FastAPI app (shares ingress + port). Wire its session-manager lifespan into `create_app`'s
  lifespan (or it errors at request time). Give `/mcp` a long Istio timeout + no buffering, like the
  existing `/ws` route.
- Auth: when `mcp_require_auth`, a Starlette middleware validates `Authorization: Bearer <JWT>`
  against the Keycloak realm JWKS before the request reaches the mount (start simple; full OAuth 2.1
  resource-server later).
- This complements the existing MCP **consumer** path (`runner/entrypoint.py::_resolve_mcp_endpoints`)
  — now DevAI is both an MCP client *and* an MCP server.

---

## End-to-end integration (DB / NATS / A2A / Redis)

`ConversationGateway.handle_turn` is the single place all integrations are touched:

1. **Identity** → resolve/attach `Principal` (Slack user, JWT sub, or session) for attribution.
2. **Session/history** → Redis, keyed by `conversation_id` (reuses the chat agent's session store).
3. **Brain** → call the existing `ChatAgent` (its 19 tools already query Postgres pipeline/SRE/audit
   tables, SCM, A2A messages, blueprints).
4. **Action turns** → "trigger a build / inject a requirement" route through the existing
   `inject_pipeline_requirements` tool + `PipelineService`, which already publish **A2A messages** and
   **NATS events** and persist to **Postgres**. Nothing new to duplicate.
5. **Audit** → every remote turn writes an `audit_log` row (who, which channel, what) — same table the
   pipeline uses.
6. **Observability** → each turn carries a `trace_id`; emits a NATS `devai.chat.*` event so the
   dashboard timeline shows remote conversations alongside pipeline runs.

So a Slack message like *"@DevAI ship the auth fix to staging"* becomes: verify → Principal(slack) →
gateway → chat agent decides → `inject_pipeline_requirements` / trigger → A2A + NATS + Postgres →
reply posted back in-thread. The remote URL and MCP paths hit the exact same gateway call.

---

## Build order (each step verified with pytest before the next)

1. **Core gateway + DTOs + Noop** (`chat/gateway.py`, `adapters/messaging/base.py`, `noop.py`) — pure,
   fully unit-testable with a fake agent. *(no new deps)*
2. **Remote URL transport** (`adapters/messaging/remote_url.py` + routes) — proves the gateway
   end-to-end over plain HTTP/SSE with zero external SDK. *(no new deps)*
3. **MCP server** (`adapters/messaging/mcp.py` + `/mcp` mount + lifespan + auth middleware). *(+`mcp`)*
4. **Slack transport** (`adapters/messaging/slack.py` + `/webhook/slack` + signature verify + async
   ack + Principal.slack). *(+`slack_sdk`)*
5. **Wire-in** (`config.py` flags, `webhook/app.py` factory + lifespan, router mounts) + **docs**
   (this file → a full SETUP guide: Slack app manifest, scopes, env, Istio VirtualService, K8s
   ExternalSecret, smoke tests).
6. **Deploy**: CI → GHCR → ArgoCD (tesserix-k8s), monitor CI green, then smoke-test each channel.

Pinned deps (late-2025): `mcp==1.9.4`, `slack_sdk==3.33.4` (+ `slack_bolt==1.21.2` only if we want
Bolt's helpers; the raw `slack_sdk` path keeps the dep surface minimal and matches our own
signature-verify code).

---

## Why this is clean (not duplicated)

- **One brain** (`ConversationGateway`) — Slack/URL/MCP are ~40-line transports each.
- **One adapter contract** — `MessagingChannel` is the family ABC; `NoopChannel` is the mandatory
  fallback; `factory.py` reads `DEVAI_*` flags and never raises (disabled channel → not mounted).
- **Reuses everything** — chat agent, Principal, Redis sessions, A2A, NATS, Postgres, the Phase-0
  signature-verify hardening pattern. New concepts added: zero.
