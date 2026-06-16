# kagent — dynamic, UI-driven model provisioning

**Status:** Shipped — 2026-06-16. Replaces the static catalog (11 standing variants)
with demand-driven Agent variants; the ModelConfig menu stays provisioned (cheap CRs).
**Goal:** a user enables models in **Settings**; kagent then **automatically provisions the
variant pods for exactly those models** — no chart edit, no redeploy — and **reaps** the
ones nobody uses. Solves the "every catalog model = a standing pod" explosion.

---

## The shift

| Today (static) | After (dynamic) |
|---|---|
| Chart `kagentModels` lists models → **every** variant is a standing pod | Chart `kagent_catalog` is the **supported menu** (operator-curated, valid ids) |
| All 11 pods run whether used or not | A variant's pod runs **iff ≥1 user enabled that model** |
| Add a model = edit chart + redeploy | Add a model to your set = **toggle in Settings** (persists), pod appears next cycle |

## Architecture

```
Settings UI (per user)        DevAI (source of truth)            agent-sync (provisioner, every 5m)
──────────────────────        ───────────────────────            ──────────────────────────────────
enable models  ───────►  kagent connector prefs.enabled_models   GET /…/kagent/active-variants
(persists in user_settings)        │  (union across ALL users     (service-token gated) ──┐
                                   │   ∩ the supported catalog)                            │
                                   └──────────────────────────►  variants = that union ◄───┘
                                                                 → export those → apply --prune
                                                                 → kagent spins up / removes pods
```

1. **Supported catalog** — `kagent_catalog` (config) is the menu of valid (provider, model)
   the platform offers. Unchanged; operator-curated.
2. **Per-user enablement** — the kagent runtime panel gets a **per-model toggle**. Enabling
   a model writes it to the user's **`kagent` connector** `prefs.enabled_models` (persists in
   `user_settings`, per-user — same store as the on/off switch).
3. **DevAI active set** — `GET /api/settings/kagent/active-variants` returns the **union** of
   every user's `enabled_models` intersected with the supported catalog → the `(suffix,
   provider, model)` variants that should exist. Service-token gated (no per-user data, no
   secrets — just "which models are wanted").
4. **agent-sync provisions** — replaces the static helm-templated `variants` with a **curl to
   DevAI's active-variants endpoint**, then exports those variants and `kubectl apply --prune`
   (the reaper already live). So a variant's Deployment/pod exists exactly when wanted, and is
   pruned when the last user disabling it lands.
5. **Dispatch** — unchanged. A user's chosen (provider, model) → its variant, which now exists
   because they enabled it; otherwise → the next in their fallback, then a Job.

**Net:** enable a model in Settings → ≤5 min later the pod is up and your runs use it; disable
it (and nobody else uses it) → pod removed. Fully dynamic, UI-driven, no redeploy, and the
catalog can be huge while only **enabled** models cost a pod.

## Build (3 repos)

- **devai (backend)** — `settings/models.py`: add `enabled_models` to the `kagent` connector
  fields; `settings/routes.py`: extend `/settings/catalog`'s `kagent` block with the user's
  `enabled_models`, and add `GET /settings/kagent/active-variants` (union, service-token gated).
- **devai (frontend)** — `dashboard/.../settings`: each model in the kagent panel becomes a
  toggle that adds/removes it from the user's `enabled_models` (via `saveConnector`).
- **tesserix-k8s** — `kagent-agent-sync`: the cronjob curls DevAI's active-variants endpoint
  (with a service token from a Secret) and uses it for `variants`; a NetworkPolicy allowing
  `kagent-system` → `devai-api`. The supported catalog (ModelConfigs) can still be chart-rendered
  (cheap CRs) — only the **Agent variants** (pods) become demand-driven.

## Decisions / safety
- **agent-sync → DevAI** over HTTP (the existing curl pattern) with a shared service token;
  alternative is DevAI writing a ConfigMap the agent-sync reads.
- **Empty/error guard** — if the active-variants call fails or returns empty, keep the LAST
  applied set (do NOT prune to zero) — same spirit as the existing non-empty-render guard.
- **ModelConfigs stay provisioned** for the whole supported catalog (cheap CRs, no pods); only
  Agent variants are demand-driven, so dispatch never races a missing ModelConfig.
- **Bounded by the supported catalog** — users can only enable what the operator curated
  (valid, direct-provider model ids), so no arbitrary/unsafe models.

## As shipped (2026-06-16)

- **devai backend** — `GET /api/settings/kagent/active-variants` returns the union of every
  ON user's `prefs.enabled_models` (∩ catalog) as a ready `param` string. A user who turned
  kagent ON but picked no model gets the **platform default** variant (`_kagent_default_model`,
  mirrors the dispatch fallback) so the switch is never live-with-nothing. Gated by reusing the
  **MCP Hub service bearer** (`identity._principal_from_service_bearer`, service role) — one
  key, one rotation, no new secret. `SettingsService.list_all_by_key` powers the cross-user union.
- **devai frontend** — the kagent panel renders each catalog model as a **toggle button**;
  clicking writes it into the user's `kagent` connector `prefs.enabled_models` via `saveConnector`.
- **tesserix-k8s** (`kagent-agent-sync`) — the cronjob's render step now **curls DevAI** for the
  active set instead of deriving `variants` from the chart; presents `DEVAI_MCP_HUB_SERVICE_TOKEN`
  (synced into kagent-system by `external-secrets/prod/kagent-system`). Three guards: fetch
  fails → init exits non-zero → no apply (keep last); empty union → **reap** all managed Agent
  variants; non-empty → export + `apply --prune`. ModelConfigs stay chart-rendered for the whole
  catalog. A NetworkPolicy entry (`istio-config` `allow-devai-ingress`) admits `kagent-system`.
- **Behaviour change on deploy** — the previously-standing 11 variants are reaped on the first
  cycle; each comes back ≤5 min after a user enables that model in Settings.
