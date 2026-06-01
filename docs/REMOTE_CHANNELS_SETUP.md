# DevAI Remote Channels — Setup & Operations Guide

Talk to DevAI from **Slack**, a **remote URL/thread**, or any **MCP client** — all routed
through one conversational brain (`ConversationGateway`), built as a clean adapter family.

This guide covers architecture, config, the Slack app setup, the MCP endpoint, the remote-URL
API, Kubernetes/Istio wiring, and end-to-end smoke tests.

---

## 1. Architecture (what was built)

```
   Slack (Events API) ─┐
                       │   POST /webhook/slack
   Remote app/URL ─────┤   POST /remote/threads/{id}/messages   ┌──────────────────────┐
                       ├──▶ GET  /remote/threads/{id}/stream  ──▶│ ConversationGateway  │
   MCP client ─────────┘   MCP  /mcp (streamable-http)           │  (one brain)         │
                                                                 │  → DevAIChatAgent    │
                                                                 │  → Redis sessions    │
                                                                 │  → A2A / NATS / PG   │
                                                                 └──────────────────────┘
```

| File | Role |
|------|------|
| `src/devai/adapters/messaging/base.py` | `MessagingChannel` ABC + `ConversationTurn`/`Reply` DTOs + gateway protocol. `dispatch()` is the never-raise boundary. |
| `src/devai/adapters/messaging/noop.py` | Mandatory Noop fallback. |
| `src/devai/adapters/messaging/factory.py` | `create_messaging_channels(settings, gateway)` — builds only enabled channels, never raises. |
| `src/devai/adapters/messaging/remote_url.py` | Remote-URL transport (request/response). |
| `src/devai/adapters/messaging/mcp.py` | MCP transport + `build_mcp_server()` (FastMCP, 4 tools). |
| `src/devai/adapters/messaging/slack.py` | Slack transport + `verify_slack_signature()`. |
| `src/devai/chat/gateway.py` | `ConversationGateway` — the single brain (wraps the chat agent + audit). |
| `src/devai/chat/messaging_service.py` | Owns the channel map + the NATS turn worker (ack-fast / reply-later). |
| `src/devai/chat/remote_routes.py` | `POST /remote/threads/{id}/messages`, `GET .../stream`. |
| `src/devai/chat/slack_routes.py` | `POST /webhook/slack` (signature verify, handshake, dedup, ack-fast). |

Everything is wired into `src/devai/webhook/app.py` (lifespan starts `MessagingService`, mounts
`/mcp`, includes the routers). Disabled channels are simply not mounted.

---

## 2. Configuration (all `DEVAI_*` env vars)

| Env var | Default | Purpose |
|---------|---------|---------|
| `DEVAI_REMOTE_CHAT_ENABLED` | `true` | Enable the remote URL/thread channel. |
| `DEVAI_MCP_SERVER_ENABLED` | `true` | Mount DevAI as an MCP server at `/mcp`. |
| `DEVAI_SLACK_ENABLED` | `false` | Enable the Slack Events API channel. |
| `DEVAI_REMOTE_CHAT_API_TOKEN` | `""` | Shared static bearer token for the remote URL **and** MCP endpoints. Empty = open (local/dev only). |
| `DEVAI_MESSAGING_USE_WORKER` | `true` | Run turns on the NATS worker (ack-fast). Required for Slack's 3 s budget. |
| `DEVAI_MESSAGING_TURN_SUBJECT` | `devai.chat.turn` | NATS subject for the turn handoff. |
| `DEVAI_SLACK_BOT_TOKEN` | `""` | Slack bot token (`xoxb-…`). **Secret Manager in prod.** |
| `DEVAI_SLACK_SIGNING_SECRET` | `""` | Slack request-signing secret. **Secret Manager in prod.** |
| `DEVAI_SLACK_ALLOWED_CHANNELS` | `""` | CSV of channel IDs allowed to use the bot; empty = all. |

Secrets (`*_TOKEN`, `*_SECRET`) go in **GCP Secret Manager** and are injected via an ExternalSecret —
never in the DB or git.

---

## 3. Remote URL / thread channel

A plain HTTP thread API any remote app can call. Static bearer token (constant-time compared).

**Send a message (sync reply):**
```bash
curl -sS -X POST https://devai.tesserix.app/remote/threads/my-thread/messages \
  -H "Authorization: Bearer $DEVAI_REMOTE_CHAT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "why did the last pipeline run for tesserix/devai fail?"}'
# -> {"reply": "...", "thread_id": "my-thread"}
```

**Stream a reply (SSE, for plain EventSource clients):**
```bash
curl -N "https://devai.tesserix.app/remote/threads/my-thread/stream?text=summarize+open+incidents" \
  -H "Authorization: Bearer $DEVAI_REMOTE_CHAT_API_TOKEN"
# -> data: {"text":"..."}  ...  data: [DONE]
```

`thread_id` is the conversation key — reuse it to continue a conversation (history persists in Redis).

---

## 4. MCP server (`/mcp`, Streamable HTTP)

DevAI is exposed **as an MCP server**, so any MCP-capable client/agent can reach the same brain.

**Endpoint:** `https://devai.tesserix.app/mcp` (Streamable HTTP transport).
**Auth:** send `Authorization: Bearer $DEVAI_REMOTE_CHAT_API_TOKEN` (when set).

**Tools exposed:**

| Tool | Args | Does |
|------|------|------|
| `chat` | `message`, `conversation_id?` | General conversation (queries pipelines/runs/repos/SRE/security, or instructs DevAI to act). |
| `pipeline_status` | `run_id` | Concise status of a pipeline run. |
| `trigger_pipeline` | `repo`, `requirements` | Ask DevAI to start work on a repo. |
| `inject_requirements` | `run_id`, `message` | Inject a requirement into a running pipeline. |

**Register with an MCP client** (example client config shape):
```json
{
  "mcpServers": {
    "devai": {
      "transport": "streamable-http",
      "url": "https://devai.tesserix.app/mcp",
      "headers": { "Authorization": "Bearer <DEVAI_REMOTE_CHAT_API_TOKEN>" }
    }
  }
}
```

> Note: DevAI already **consumes** MCP servers in its agent runner
> (`runner/entrypoint.py::_resolve_mcp_endpoints`). This adds the **server** side — DevAI is now
> both an MCP client and an MCP server.

---

## 5. Slack channel (Events API)

### 5.1 Create the Slack app

1. https://api.slack.com/apps → **Create New App** → **From scratch**.
2. **OAuth & Permissions → Bot Token Scopes**: add
   - `app_mentions:read`
   - `chat:write`
   - `im:history`, `im:read` *(only if you want DM support)*
3. **Event Subscriptions → Enable Events**
   - **Request URL:** `https://devai.tesserix.app/webhook/slack`
     (Slack will POST a `url_verification` challenge; the endpoint echoes it — you'll see "Verified".)
   - **Subscribe to bot events:** `app_mention` (+ `message.im` for DMs).
4. **Install App** to the workspace → copy the **Bot User OAuth Token** (`xoxb-…`).
5. **Basic Information → App Credentials** → copy the **Signing Secret**.

App-manifest equivalent (paste under **App Manifest**):
```yaml
display_information:
  name: DevAI
features:
  bot_user:
    display_name: DevAI
    always_online: true
oauth_config:
  scopes:
    bot: [app_mentions:read, chat:write, im:history, im:read]
settings:
  event_subscriptions:
    request_url: https://devai.tesserix.app/webhook/slack
    bot_events: [app_mention, message.im]
```

### 5.2 Provide the secrets

Put the two secrets in GCP Secret Manager and reference them from the DevAI ExternalSecret:
```
DEVAI_SLACK_BOT_TOKEN       <- secret: devai-slack-bot-token
DEVAI_SLACK_SIGNING_SECRET  <- secret: devai-slack-signing-secret
```
Set `DEVAI_SLACK_ENABLED=true`. (If either secret is missing, the Slack channel is skipped with a
warning — it never crashes the pod.)

### 5.3 UX

- In any channel the bot is in: `@DevAI why did run abc123 fail?` → DevAI replies **in-thread**.
- The bot acks Slack within 3 s, runs the turn on the NATS worker, then posts the answer back via
  `chat.postMessage(thread_ts=…)`. Retried deliveries are de-duped on `event_id` (Redis, 10-min TTL).

---

## 6. Kubernetes / Istio

The MCP `/mcp` GET stream and the remote `/stream` SSE are long-lived — give them generous timeouts
and disable response buffering on the VirtualService (same pattern as the existing `/ws` route):

```yaml
# tesserix-k8s ... devai VirtualService (excerpt)
http:
  - match:
      - uri: { prefix: /mcp }
      - uri: { prefix: /remote/threads }
    route:
      - destination: { host: devai-api.devai.svc.cluster.local, port: { number: 8080 } }
    timeout: 0s            # no hard timeout on the streaming routes
  - match:
      - uri: { prefix: /webhook/slack }
    route:
      - destination: { host: devai-api.devai.svc.cluster.local, port: { number: 8080 } }
    timeout: 5s            # Slack only needs the fast ack
```

ExternalSecret (excerpt) — add the Slack keys alongside the existing DevAI secrets:
```yaml
- secretKey: DEVAI_SLACK_BOT_TOKEN
  remoteRef: { key: devai-slack-bot-token }
- secretKey: DEVAI_SLACK_SIGNING_SECRET
  remoteRef: { key: devai-slack-signing-secret }
- secretKey: DEVAI_REMOTE_CHAT_API_TOKEN
  remoteRef: { key: devai-remote-chat-api-token }
```

All cluster changes go through the `tesserix-k8s` repo + ArgoCD (no manual `kubectl apply`).

---

## 7. End-to-end smoke tests

```bash
# 1. Remote URL — sync
curl -sS -X POST $BASE/remote/threads/smoke/messages \
  -H "Authorization: Bearer $DEVAI_REMOTE_CHAT_API_TOKEN" \
  -d '{"text":"hello devai"}'

# 2. Remote URL — auth is enforced (expect 401)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/remote/threads/smoke/messages -d '{"text":"hi"}'

# 3. MCP — list tools with an MCP client pointed at $BASE/mcp (expect: chat, pipeline_status, ...)

# 4. Slack — in the workspace: "@DevAI ping"  (expect an in-thread reply)
#    Health: the Slack request URL shows "Verified" in the Slack app config.
```

Local (no LLM / no NATS): the unit suites prove the wiring —
`tests/unit/test_messaging_gateway.py` (gateway + channels + factory) and
`tests/unit/test_messaging_routes.py` (remote-URL auth + Slack signature/handshake/enqueue).

---

## 8. Design notes

- **One brain, thin transports.** All conversational logic lives in `ConversationGateway`; Slack,
  remote-URL, and MCP are ~40–80-line transports. Zero duplication.
- **Never-raise.** `MessagingChannel.dispatch()` catches everything and degrades to noop, so a bad
  turn never crashes a Slack ack / MCP call / HTTP request. The factory skips unconstructable
  channels. Matches the adapter contract (CLAUDE.md §6).
- **Ack-fast.** Slack turns hand off to the NATS worker (`messaging_use_worker`), so the HTTP ack is
  instant; the worker replies later in-thread. Falls back to an in-process task if NATS is down.
- **Integrated.** Turns thread a `Principal` (audit + A2A attribution), persist history in Redis,
  write an `audit_log` row, and reuse the chat agent's 19 tools (which already touch Postgres, SCM,
  A2A, blueprints). Adding a new transport is one file.
