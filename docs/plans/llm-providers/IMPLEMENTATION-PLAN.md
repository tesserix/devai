# Multi-Provider LLM Plan — Discovery, Per-User Keys, Groq/OpenRouter

**Status:** Implementing — 2026-06-12
**Companion:** `docs/plans/vertex-multi-model/IMPLEMENTATION-PLAN.md` (Vertex plane, tiers,
per-spec routing — shipped), `docs/SETTINGS_CAPABILITY.md` (per-user connectors — shipped).

## 1. Goals

1. **Every provider behind the same adapter family** — add `groq` and `openrouter` backends
   (both OpenAI-wire-compatible) next to anthropic / openai / vertex_gemini / gateway / noop.
2. **Model discovery as a first-class API** — one authenticated endpoint that answers
   "which models can I use on provider X?", evaluated against the **caller's own keys**
   (Settings overlay), falling back to platform credentials only when the user has none.
3. **Per-user keys are the rule, not the exception** — a user who sets their own secret in
   Settings always runs on it (already shipped); a new opt-in policy flag can *require* a
   user connector so the shared platform secret is never used for interactive principals.
4. **UI** — the Settings connector editor offers real model choices per provider, fetched
   from the discovery endpoint; key fields per provider appear automatically (catalog-driven).

## 2. Design

### 2.1 Adapters (src/devai/adapters/llm/)

| Provider | Implementation | Settings |
|---|---|---|
| `groq` | `OpenAILLMAdapter` @ `https://api.groq.com/openai/v1` | `groq_api_key`, `groq_model` (existed) |
| `openrouter` | `OpenAILLMAdapter` @ `https://openrouter.ai/api/v1` | `openrouter_api_key`, `openrouter_model` (new) |

Spec alias map gains `groq → groq`, `openrouter → openrouter` (registered backends now);
`nemoclaw` stays unmapped → default adapter.

### 2.2 list_models() on the ABC

`LLMAdapter.list_models() -> list[{"id","display_name"}]`, default `[]`.
- `OpenAILLMAdapter`: `GET /models` via the SDK — works identically for OpenAI, Groq,
  OpenRouter, and the gateway (whatever the base_url serves).
- `AnthropicLLMAdapter`: `client.models.list()` (the `/v1/models` API).
- `VertexGeminiLLMAdapter`: publisher-catalog REST (google + anthropic publishers),
  GA/preview text models only.
- Noop/gateway-without-URL: `[]`. Never raises — discovery failures return `[]` + a log.

### 2.3 Discovery API (settings router)

`GET /api/settings/models/{provider}` — authenticated principal required.
1. Build the caller's overlay (`build_overlay`) — their keys win, platform creds fallback.
2. `create_llm_adapter(overlay, provider=provider)`; unknown provider → 400; unconfigured
   → 200 with `{"models": [], "configured": false}` so the UI can prompt for a key.
3. Response: `{"provider", "configured", "models":[{id,display_name}]}`. 60s in-process
   TTL cache keyed (provider, has-user-key) to keep the UI snappy without hammering APIs.
4. Secrets never appear in responses; the adapter is built and discarded (closed).

### 2.4 Per-user-only policy

`DEVAI_LLM_REQUIRE_USER_CONNECTOR` (default `false`). When `true`,
`StageDeps.llm_for_principal()` returns **None** for human principals without their own
LLM connector instead of the platform default — the run stubs with a clear "configure
your LLM connector in Settings" message. System/webhook principals (`auth_provider` in
webhook/system, or non-email triggered_by) keep the platform adapter so automation never
breaks. Default off = today's behavior.

### 2.5 UI

Settings connector editor: for the `llm` connector, fetch
`/api/settings/models/{selected provider}` on provider change and attach the result as a
`<datalist>` to model text fields (`*_model` keys) — free-text stays allowed (datalist,
not select), so gateway aliases and brand-new models still work.

## 3. Testing

- Factory: groq/openrouter degrade to noop without keys; registered in KNOWN_PROVIDERS;
  alias resolution updated (groq no longer falls back).
- Discovery route: 401 unauth; 400 unknown provider; configured=false without creds;
  models returned with a stubbed adapter; no secret material in response.
- list_models: noop returns []; vertex parses the catalog shape.

## 4. Deploy

devai only (no infra changes): commit → push → CI → auto-roll. Verify on the pod:
catalog providers include groq/openrouter; `GET /api/settings/models/...` answers 401
unauthenticated; in-pod authenticated call via the service returns the Vertex/Anthropic
model lists.

## 5. Current model inventory (validated 2026-06-12)

- **Anthropic direct API** (platform key): claude-fable-5, opus-4-8, opus-4-7, opus-4-6,
  sonnet-4-6, opus-4-5-20251101, haiku-4-5-20251001, sonnet-4-5-20250929,
  opus-4-1-20250805, opus-4-20250514, sonnet-4-20250514.
- **Vertex Gemini** (serving): 2.5-flash, 2.5-flash-lite, 2.5-pro, 3.5-flash,
  3.1-flash-lite, 3.1-pro-preview, 3-flash-preview.
- **Vertex Claude**: opus-4-6 serving; fable-5/opus-4-8/opus-4-7 enabled pending quota
  grants; sonnets/haiku pending Marketplace purchase.
- **OpenAI** (platform key via gateway): gpt-4.1, o3.
- **Groq / OpenRouter**: enabled by this plan once a key is set (platform or per-user).
