"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ChevronDown, KeyRound, Loader2, Pencil, Plus, Save, Store, Trash2, Users } from "lucide-react";
import { useConfirm } from "@/components/confirm-dialog";
import { LlmCapabilitiesPanel } from "@/components/llm-capabilities-panel";
import {
  api,
  type McpMarketplaceEntry,
  type SettingsCatalog,
  type KagentCatalog,
  type KagentModelState,
  type SettingsConnector,
  type SettingsConnectorSpec,
  type SharedConnector,
  type TeamMember,
  type TeamSummary,
  type WritableScope,
} from "@/lib/api";
import { GuidancePanel } from "@/components/guidance";

/**
 * Settings — per-user / per-tenant connectors + secrets.
 *
 * Connectors (LLM, SCM, Memory, Slack, MCP, …) are configured per scope
 * (user / team / tenant / global). Secret fields are write-only: their values
 * go straight to GCP Secret Manager via the backend and are never read back —
 * the UI only shows whether a secret is set. Resolution at runtime is
 * user → team → tenant → global, so your own keys drive your chat + pipeline
 * runs.
 *
 * Theme: tokens only (var(--ink-*), var(--surface-*)) — no raw Tailwind grays.
 */

export default function SettingsPage() {
  const [catalog, setCatalog] = useState<SettingsCatalog | null>(null);
  const [connectors, setConnectors] = useState<SettingsConnector[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null); // connector key being added/edited
  const [trial, setTrial] = useState<Awaited<ReturnType<typeof api.getTrialStatus>> | null>(null);
  // When the user clicks "Connect" in the MCP marketplace, pre-fill the MCP
  // connector form (name + endpoint) and open it.
  const [mcpPrefill, setMcpPrefill] = useState<
    { provider?: string; instanceId?: string; values?: Record<string, string> } | null
  >(null);
  // Scopes the caller can write (Just me / teams + orgs they admin) and which
  // connectors are already provided by a broader (team/org) scope.
  const [writableScopes, setWritableScopes] = useState<WritableScope[]>([]);
  const [shared, setShared] = useState<Record<string, SharedConnector>>({});
  const [capsRefresh, setCapsRefresh] = useState(0); // bumps after each load → refetch LLM routing

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cat, mine] = await Promise.all([api.getSettingsCatalog(), api.listSettings()]);
      setCatalog(cat);
      setConnectors(mine.connectors);
      setWritableScopes(mine.writable_scopes ?? [{ scope: "user", scope_id: "", label: "Just me" }]);
      setShared(mine.shared ?? {});
      setCapsRefresh((k) => k + 1); // re-resolve LLM routing against the new connectors
      // Trial state is advisory — never block the page on it.
      api.getTrialStatus().then(setTrial).catch(() => setTrial(null));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load settings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Feedback after the OAuth redirect lands back on /settings?mcp_oauth=…
  const [oauthMsg, setOauthMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const p = new URLSearchParams(window.location.search);
    const status = p.get("mcp_oauth");
    if (!status) return;
    if (status === "connected") {
      setOauthMsg({ ok: true, text: `Connected ${p.get("server") || "MCP server"} via OAuth.` });
    } else if (status === "error") {
      setOauthMsg({ ok: false, text: `OAuth failed: ${p.get("detail") || "unknown error"}.` });
    }
    window.history.replaceState({}, "", window.location.pathname);
  }, []);

  const secretsWritable = catalog?.secrets_writable ?? false;

  return (
    <div className="p-7 w-full">
      <div className="label-eyebrow">Mission control</div>
      <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1">Settings</h1>
      <p className="text-sm text-[var(--ink-300)] mt-1">
        Connect your own LLM, source control, memory, Slack and MCP servers. Your credentials drive
        both your conversations and your pipeline runs.
      </p>

      <GuidancePanel id="settings" className="mt-5" />

      {trial?.applicable && trial.exhausted && (
        <div className="panel mt-5 p-4 text-sm flex items-start gap-3 border border-red-500/40">
          <AlertTriangle className="w-5 h-5 text-red-400 mt-0.5 shrink-0" />
          <div>
            <p className="text-[var(--ink-100)] font-medium">Free trial used up — add your own API key to continue.</p>
            <p className="text-[var(--ink-300)] mt-1">
              You&apos;ve spent your {trial.budget.toLocaleString()}-token trial allowance. The shared
              platform credentials are permanently revoked for your account — configure an LLM
              connector below (Anthropic, Vertex, OpenAI, Groq or OpenRouter) to keep working.
            </p>
          </div>
        </div>
      )}
      {trial?.applicable && !trial.exhausted && trial.trial_enabled && (
        <div
          className={`panel mt-5 p-4 text-sm flex items-start gap-3 border ${
            trial.warning ? "border-amber-500/40" : "border-sky-500/30"
          }`}
        >
          <AlertTriangle className={`w-5 h-5 mt-0.5 shrink-0 ${trial.warning ? "text-amber-400" : "text-sky-400"}`} />
          <div className="flex-1">
            <p className="text-[var(--ink-100)]">
              {trial.warning ? "Trial almost used up" : "You're on the free trial"} —{" "}
              {trial.remaining.toLocaleString()} of {trial.budget.toLocaleString()} tokens left.
            </p>
            <p className="text-[var(--ink-300)] mt-1">
              Add your own LLM API key below before it runs out; once spent, the shared credentials
              are revoked permanently for your account.
            </p>
            <div className="mt-2 h-1.5 rounded bg-[var(--surface-border)] overflow-hidden">
              <div
                className={trial.warning ? "h-full bg-amber-400" : "h-full bg-sky-400"}
                style={{ width: `${Math.min(100, Math.round((trial.used / Math.max(1, trial.budget)) * 100))}%` }}
              />
            </div>
          </div>
        </div>
      )}

      {!secretsWritable && !loading && (
        <div className="panel mt-5 p-4 text-sm flex items-start gap-3 border border-amber-500/30">
          <AlertTriangle className="w-5 h-5 text-amber-400 mt-0.5 shrink-0" />
          <div>
            <p className="text-[var(--ink-100)]">Secret storage is read-only.</p>
            <p className="text-[var(--ink-300)] mt-1">
              The secrets backend can&apos;t provision new values yet — set{" "}
              <code>DEVAI_SECRETS_PROVIDER=gcp_sm</code> and grant the devai service account
              Secret Manager write access. You can still set non-secret preferences.
            </p>
          </div>
        </div>
      )}

      {error && (
        <div className="panel mt-5 p-4 text-sm text-red-300 border border-red-500/30">{error}</div>
      )}

      {oauthMsg && (
        <div
          className={`panel mt-5 p-4 text-sm flex items-center gap-2 border ${
            oauthMsg.ok ? "border-emerald-500/30 text-emerald-300" : "border-red-500/30 text-red-300"
          }`}
        >
          {oauthMsg.ok ? <Check className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
          {oauthMsg.text}
        </div>
      )}

      {loading ? (
        <div className="mt-8 flex items-center gap-2 text-[var(--ink-300)]">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading…
        </div>
      ) : (
        <div className="mt-6 space-y-4">
          <TeamOrgPanel onChanged={() => void load()} />
          <LlmCapabilitiesPanel refreshKey={capsRefresh} />
          {catalog?.kagent && (
            <KagentRuntimePanel kagent={catalog.kagent} onChanged={() => void load()} />
          )}
          {catalog?.connectors.map((spec) => {
            const configured = connectors.filter((c) => c.connector_key === spec.key);
            return (
              <ConnectorCard
                key={spec.key}
                spec={spec}
                configured={configured}
                secretsWritable={secretsWritable}
                isEditing={editing === spec.key}
                onEdit={() => {
                  if (editing !== spec.key) setMcpPrefill(null);
                  setEditing(editing === spec.key ? null : spec.key);
                }}
                onSaved={() => {
                  setEditing(null);
                  setMcpPrefill(null);
                  void load();
                }}
                onDeleted={() => void load()}
                prefill={spec.key === "mcp" ? mcpPrefill ?? undefined : undefined}
                writableScopes={writableScopes}
                sharedBy={shared[spec.key]}
              />
            );
          })}

          <McpMarketplace
            onConnect={async (entry) => {
              // OAuth servers run the consent flow (redirect to the provider);
              // everything else pre-fills the MCP connector form.
              if (entry.auth_kind === "oauth") {
                try {
                  const { authorize_url } = await api.mcpOAuthStart(entry.name);
                  if (authorize_url) window.location.href = authorize_url;
                } catch (e) {
                  setError(e instanceof Error ? e.message : "Could not start OAuth");
                }
                return;
              }
              setMcpPrefill({
                provider: entry.transport === "sse" ? "sse" : "streamable_http",
                instanceId: entry.name,
                values: { mcp_name: entry.display_name || entry.name, mcp_url: entry.endpoint },
              });
              setEditing("mcp");
              if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
            }}
          />
        </div>
      )}
    </div>
  );
}

/** Team & Org — create a team, tag it with an org, manage members. Once you
 *  have a team/org you admin, connectors gain "Apply to: Team/Org" so you can
 *  share a credential with everyone instead of each person setting their own. */
const KAGENT_PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  vertex: "Vertex",
  vertex_gemini: "Vertex",
  gemini: "Gemini",
  groq: "Groq",
  bedrock: "Bedrock",
};
const kagentProviderLabel = (p: string) => KAGENT_PROVIDER_LABELS[p.toLowerCase()] ?? p;

function KagentRuntimePanel({ kagent, onChanged }: { kagent: KagentCatalog; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Record<string, KagentModelState>>({});
  // null = not yet known; false = no agent is labelled for kagent (dormant) → runs
  // use on-demand Jobs and enabling a model here provisions nothing.
  const [configured, setConfigured] = useState<boolean | null>(null);
  // Group the catalog models by provider so the user sees which provider/model
  // combos kagent can run their agents on (with their own key, via passthrough).
  const byProvider = new Map<string, string[]>();
  for (const m of kagent.models) {
    const list = byProvider.get(m.provider) ?? [];
    list.push(m.model);
    byProvider.set(m.provider, list);
  }
  const enabledCount = kagent.enabled_models.length;
  const allModels = kagent.models.map((m) => m.model);
  // "Recommended" = the first model of each provider — balanced fallback coverage
  // across providers without a pod per model.
  const recommended = (() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const m of kagent.models) {
      if (!seen.has(m.provider)) {
        seen.add(m.provider);
        out.push(m.model);
      }
    }
    return out;
  })();

  // Poll live per-model pod status while kagent is on, so the user SEES a pod
  // come up after enabling (there's a ~couple-min provisioning lag). Degrades
  // silently — soft fetch returns nothing when the controller is unreachable.
  useEffect(() => {
    if (!kagent.enabled) {
      setStatus({});
      setConfigured(null);
      return;
    }
    let alive = true;
    const tick = async () => {
      const r = await api.kagentRuntimeStatus();
      if (alive && r) {
        setStatus(r.models);
        setConfigured(r.agents_configured);
      }
    };
    void tick();
    const id = setInterval(() => void tick(), 8000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [kagent.enabled, kagent.enabled_models.join(",")]);
  // The on/off switch and the per-model selection both persist per-user via the
  // `kagent` connector (provider on/off + prefs.enabled_models). Enabling a model
  // makes kagent-agent-sync provision that variant's pod automatically (and reap
  // it when nobody has it enabled) — fully dynamic, no redeploy.
  const save = async (provider: string, enabledModels: string[]) => {
    setBusy(true);
    try {
      await api.saveConnector({
        scope: "user",
        scope_id: "",
        connector_key: "kagent",
        provider,
        prefs: { enabled_models: enabledModels },
      });
      onChanged();
    } finally {
      setBusy(false);
    }
  };
  const toggle = () => void save(kagent.enabled ? "off" : "on", kagent.enabled_models);
  const toggleModel = (model: string) =>
    void save(
      kagent.enabled ? "on" : "off",
      kagent.enabled_models.includes(model)
        ? kagent.enabled_models.filter((m) => m !== model)
        : [...kagent.enabled_models, model],
    );
  const applyPreset = (models: string[]) => void save(kagent.enabled ? "on" : "off", models);
  // Effective per-model state: prefer live pod status; else fall back to intent.
  const stateOf = (model: string): KagentModelState => {
    if (!kagent.enabled_models.includes(model)) return "off";
    return status[model] && status[model] !== "off" ? status[model] : "enabled";
  };
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">kagent runtime</h3>
          {kagent.enabled && enabledCount > 0 && (
            <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-600">
              {enabledCount} model{enabledCount === 1 ? "" : "s"} enabled
            </span>
          )}
        </div>
        <button
          onClick={() => void toggle()}
          disabled={busy}
          role="switch"
          aria-checked={kagent.enabled}
          title={kagent.enabled ? "Disable kagent for your runs" : "Enable kagent for your runs"}
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 ${
            kagent.enabled
              ? "bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/25"
              : "bg-muted text-muted-foreground hover:bg-muted/70"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${kagent.enabled ? "bg-emerald-500" : "bg-muted-foreground/50"}`} />
          {busy ? "saving…" : kagent.enabled ? "On" : "Off"}
        </button>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        Long-lived agents run on standing pods instead of one-shot jobs. Click a model to{" "}
        <strong>enable</strong> it — kagent spins up that model&apos;s pod automatically within a couple of
        minutes (and reaps it when nobody&apos;s using it). Runs use <strong>your own key</strong> and your
        chosen model, falling back across the others you enabled.
      </p>
      {kagent.models.length === 0 ? (
        <p className="mt-3 text-xs text-muted-foreground">No kagent models configured.</p>
      ) : (
        <div className="mt-3">
          <div className="mb-2 flex items-center gap-1.5 text-[11px]">
            <span className="text-muted-foreground">Quick set:</span>
            <button
              onClick={() => applyPreset(recommended)}
              disabled={busy}
              title="One model per provider — balanced fallback coverage"
              className="rounded border border-border px-1.5 py-0.5 text-foreground/80 transition hover:bg-muted disabled:opacity-50"
            >
              Recommended
            </button>
            <button
              onClick={() => applyPreset(allModels)}
              disabled={busy}
              className="rounded border border-border px-1.5 py-0.5 text-foreground/80 transition hover:bg-muted disabled:opacity-50"
            >
              All
            </button>
            <button
              onClick={() => applyPreset([])}
              disabled={busy || enabledCount === 0}
              className="rounded border border-border px-1.5 py-0.5 text-foreground/80 transition hover:bg-muted disabled:opacity-50"
            >
              Clear
            </button>
          </div>
          {!kagent.enabled && (
            <p className="mb-2 rounded-md bg-muted/60 px-2.5 py-1.5 text-[11px] text-muted-foreground">
              kagent is <strong>off</strong> — your picks are saved, but no pods run until you switch it{" "}
              <span className="text-emerald-600">On</span> above.
            </p>
          )}
          {kagent.enabled && configured === false && (
            <p className="mb-2 rounded-md bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-700 ring-1 ring-amber-500/30">
              No agent is currently set to use kagent, so your runs execute <strong>on-demand as
              Jobs</strong> (spun up per run, then torn down) — these picks provision nothing yet.
              kagent runs a standing pod only once an agent is labelled for it (an admin step), which
              is reserved for a constantly-hit agent with spare node capacity.
            </p>
          )}
          <div className={`space-y-2.5 ${kagent.enabled ? "" : "opacity-60"}`}>
            {Array.from(byProvider.entries()).map(([provider, models]) => {
              const onCount = models.filter((m) => kagent.enabled_models.includes(m)).length;
              return (
                <div key={provider} className="flex flex-wrap items-center gap-2">
                  <span className="w-20 shrink-0 text-xs font-medium text-foreground/80">
                    {kagentProviderLabel(provider)}
                    {onCount > 0 && <span className="ml-1 text-[10px] text-emerald-600">{onCount}</span>}
                  </span>
                  {models.map((model) => {
                    const st = stateOf(model);
                    const on = st !== "off";
                    const tip =
                      st === "running"
                        ? "Running — pod is up; click to disable"
                        : st === "provisioning"
                          ? "Provisioning — pod spinning up (~couple min); click to disable"
                          : on
                            ? "Enabled — click to disable"
                            : "Click to enable";
                    return (
                      <button
                        key={model}
                        onClick={() => toggleModel(model)}
                        disabled={busy}
                        title={tip}
                        className={`inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs transition disabled:opacity-50 ${
                          st === "provisioning"
                            ? "bg-amber-500/15 text-amber-600 ring-1 ring-amber-500/40 hover:bg-amber-500/25"
                            : on
                              ? "bg-emerald-500/15 text-emerald-600 ring-1 ring-emerald-500/40 hover:bg-emerald-500/25"
                              : "bg-muted text-muted-foreground ring-1 ring-transparent hover:bg-muted/70 hover:ring-border"
                        }`}
                      >
                        {st === "running" ? (
                          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                        ) : st === "provisioning" ? (
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />
                        ) : (
                          <span className={on ? "text-emerald-500" : "text-muted-foreground/40"}>
                            {on ? "✓" : "+"}
                          </span>
                        )}
                        {model}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
          {kagent.enabled && Object.keys(status).length > 0 && (
            <div className="mt-2 flex items-center gap-3 text-[10px] text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> running
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" /> provisioning
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="text-emerald-500">✓</span> enabled
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TeamOrgPanel({ onChanged }: { onChanged: () => void }) {
  const confirm = useConfirm();
  const [teams, setTeams] = useState<TeamSummary[] | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [orgId, setOrgId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [manage, setManage] = useState<string | null>(null); // team id whose members are open

  const load = useCallback(() => {
    api.listMyTeams().then(setTeams).catch(() => setTeams([]));
  }, []);
  useEffect(() => {
    if (open && teams === null) load();
  }, [open, teams, load]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.createTeam(name.trim(), orgId.trim());
      setName("");
      setOrgId("");
      load();
      onChanged(); // refresh Settings so the new team/org shows in "Apply to"
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not create team");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel p-5">
      <button className="flex items-center justify-between w-full gap-4" onClick={() => setOpen((v) => !v)}>
        <div className="text-left">
          <div className="flex items-center gap-2">
            <Users className="w-4 h-4 text-indigo-400" />
            <h2 className="text-[var(--ink-50)] font-medium">Team &amp; Org</h2>
            {teams && <span className="pill text-xs">{teams.length}</span>}
          </div>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Set up a team and tag it with your org (e.g. <code>tesserix</code>). Then any connector below gets an
            <span className="text-[var(--ink-100)]"> Apply to: Team / Org</span> option — share a GitHub App or key
            once and everyone inherits it. You become the team admin.
          </p>
        </div>
        <ChevronDown className={`w-4 h-4 text-[var(--ink-400)] transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="mt-4 space-y-4">
          {/* Create */}
          <div className="rounded-md border border-[var(--surface-border)] bg-[var(--surface-raised)] p-3">
            <div className="label-eyebrow mb-2">Create a team</div>
            <div className="grid sm:grid-cols-3 gap-2">
              <input className="field text-sm" placeholder="Team name (e.g. Platform)" value={name} onChange={(e) => setName(e.target.value)} />
              <input className="field text-sm" placeholder="Org id (e.g. tesserix)" value={orgId} onChange={(e) => setOrgId(e.target.value)} />
              <button className="btn-secondary text-sm flex items-center justify-center gap-1" disabled={busy || !name.trim()} onClick={create}>
                <Plus className="w-3.5 h-3.5" /> Create team
              </button>
            </div>
            <p className="text-[11px] text-[var(--ink-400)] mt-1.5">
              The org id is the umbrella all your teams share — connectors saved at org scope reach every team in it.
            </p>
            {err && <p className="text-xs text-red-300 mt-1">{err}</p>}
          </div>

          {/* Your teams */}
          {teams && teams.length > 0 ? (
            <div className="space-y-2">
              <div className="label-eyebrow">Your teams</div>
              {teams.map((t) => (
                <div key={t.id} className="rounded-md border border-[var(--surface-border)] bg-[var(--surface-raised)] p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-[var(--ink-100)] text-sm font-medium truncate">{t.name}</span>
                      {t.org_id && <span className="pill text-[10px]">org: {t.org_id}</span>}
                      {t.is_admin && <span className="pill text-[10px] text-sky-400">admin</span>}
                      <span className="text-[10px] text-[var(--ink-400)]">{t.member_count ?? 0} member(s)</span>
                    </div>
                    {t.is_admin && (
                      <button className="btn-secondary text-xs" onClick={() => setManage(manage === t.id ? null : t.id)}>
                        Members
                      </button>
                    )}
                  </div>
                  {manage === t.id && <TeamMembers teamId={t.id} confirm={confirm} onChanged={onChanged} />}
                </div>
              ))}
            </div>
          ) : (
            teams && <p className="text-sm text-[var(--ink-400)]">You're not in any team yet — create one above.</p>
          )}
        </div>
      )}
    </div>
  );
}

/** Members manager for one team (admin only). Add by email/uid + role, remove. */
function TeamMembers({
  teamId,
  confirm,
  onChanged,
}: {
  teamId: string;
  confirm: ReturnType<typeof useConfirm>;
  onChanged: () => void;
}) {
  const [members, setMembers] = useState<TeamMember[] | null>(null);
  const [uid, setUid] = useState("");
  const [admin, setAdmin] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.teamMembers(teamId).then(setMembers).catch(() => setMembers([]));
  }, [teamId]);
  useEffect(() => {
    load();
  }, [load]);

  const add = async () => {
    if (!uid.trim()) return;
    setBusy(true);
    try {
      await api.addTeamMember(teamId, uid.trim(), admin ? ["admin"] : ["developer"]);
      setUid("");
      setAdmin(false);
      load();
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 pt-3 border-t border-[var(--surface-border)] space-y-2">
      <div className="grid sm:grid-cols-[1fr_auto_auto] gap-2 items-center">
        <input className="field text-sm" placeholder="Member email or uid" value={uid} onChange={(e) => setUid(e.target.value)} />
        <label className="text-xs text-[var(--ink-300)] flex items-center gap-1.5">
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} /> admin
        </label>
        <button className="btn-secondary text-xs flex items-center gap-1" disabled={busy || !uid.trim()} onClick={add}>
          <Plus className="w-3 h-3" /> Add
        </button>
      </div>
      {members?.map((m) => (
        <div key={m.user_uid} className="flex items-center justify-between text-sm rounded px-2 py-1.5 bg-[var(--surface-base)]">
          <span className="text-[var(--ink-100)] truncate">
            {m.user_uid}
            {(m.roles || []).includes("admin") && <span className="pill text-[10px] text-sky-400 ml-2">admin</span>}
          </span>
          <button
            className="text-[var(--ink-400)] hover:text-red-400 p-1"
            title="Remove member"
            onClick={async () => {
              const ok = await confirm({ title: `Remove ${m.user_uid}?`, message: "They lose access to this team's shared connectors.", confirmLabel: "Remove", tone: "danger" });
              if (!ok) return;
              await api.removeTeamMember(teamId, m.user_uid);
              load();
              onChanged();
            }}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}

/** MCP marketplace — browse the registry's MCP servers and connect your own. */
function McpMarketplace({ onConnect }: { onConnect: (e: McpMarketplaceEntry) => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.mcpMarketplace>> | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api
      .mcpMarketplace()
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (open && data === null) load();
  }, [open, data, load]);

  const connectable = data?.connectable ?? [];
  const builtin = data?.builtin ?? [];
  const total = connectable.length + builtin.length;

  const [query, setQuery] = useState("");
  const [cat, setCat] = useState("all");
  const categories = useMemo(() => {
    const s = new Set<string>();
    connectable.forEach((e) => e.category && s.add(e.category));
    return ["all", ...Array.from(s).sort()];
  }, [connectable]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return connectable.filter(
      (e) =>
        (cat === "all" || e.category === cat) &&
        (!q || `${e.display_name} ${e.name} ${e.description} ${e.category}`.toLowerCase().includes(q))
    );
  }, [connectable, query, cat]);

  return (
    <div className="panel p-5">
      <button className="flex items-center justify-between w-full gap-4" onClick={() => setOpen((v) => !v)}>
        <div className="text-left">
          <div className="flex items-center gap-2">
            <Store className="w-4 h-4 text-indigo-400" />
            <h2 className="text-[var(--ink-50)] font-medium">MCP Marketplace</h2>
            {data && <span className="pill text-xs">{total} apps</span>}
          </div>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Connect popular tools — GitHub, Jira, Notion, Slack, Figma, draw.io, Postgres and more.
            Their tools become available to your agents and chat. Credentials stay in your own scope.
          </p>
        </div>
        <ChevronDown className={`w-4 h-4 text-[var(--ink-400)] transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="mt-4">
          {loading && (
            <div className="flex items-center gap-2 text-[var(--ink-300)] text-sm">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading marketplace…
            </div>
          )}

          {!loading && total > 0 && (
            <>
              <div className="flex flex-wrap items-center gap-2 mb-3">
                <input
                  className="field text-sm flex-1 min-w-[180px]"
                  placeholder="Search apps…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <select className="field text-sm" value={cat} onChange={(e) => setCat(e.target.value)}>
                  {categories.map((c) => (
                    <option key={c} value={c}>
                      {c === "all" ? "All categories" : c}
                    </option>
                  ))}
                </select>
              </div>

              <div className="label-eyebrow mb-2">Connect your own ({filtered.length})</div>
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {filtered.map((e) => (
                  <McpCard key={e.name} entry={e} onConnect={() => onConnect(e)} />
                ))}
              </div>

              {builtin.length > 0 && (
                <>
                  <div className="label-eyebrow mb-2 mt-5">Built-in (always on)</div>
                  <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                    {builtin.map((e) => (
                      <McpCard key={e.name} entry={e} builtin />
                    ))}
                  </div>
                </>
              )}
            </>
          )}

          {!loading && total === 0 && (
            <p className="text-sm text-[var(--ink-400)]">No MCP servers in the registry yet.</p>
          )}
        </div>
      )}
    </div>
  );
}

const AUTH_BADGE: Record<string, { label: string; cls: string }> = {
  oauth: { label: "OAuth", cls: "text-sky-400" },
  token: { label: "API key", cls: "text-amber-400" },
  env: { label: "Secret", cls: "text-amber-400" },
  none: { label: "No auth", cls: "text-emerald-400" },
};

function McpCard({
  entry,
  onConnect,
  builtin,
}: {
  entry: McpMarketplaceEntry;
  onConnect?: () => void;
  builtin?: boolean;
}) {
  const auth = AUTH_BADGE[entry.auth_kind] ?? null;
  return (
    <div className="rounded-md border border-[var(--surface-border)] bg-[var(--surface-raised)] p-3 flex flex-col">
      <div className="flex items-start justify-between gap-2">
        <div className="text-[var(--ink-100)] text-sm font-medium">{entry.display_name || entry.name}</div>
        {entry.category && <span className="pill text-[10px] capitalize">{entry.category}</span>}
      </div>
      <p className="text-xs text-[var(--ink-300)] mt-1 line-clamp-3 flex-1">{entry.description}</p>
      <div className="flex items-center justify-between mt-2">
        <span className="text-[10px] text-[var(--ink-400)] flex items-center gap-1.5">
          {auth && <span className={auth.cls}>{auth.label}</span>}
          {entry.native === "stdio" && <span title="runs via the MCP bridge">· bridge</span>}
          {builtin && entry.tool_count > 0 && <span>· {entry.tool_count} tools</span>}
        </span>
        {builtin ? (
          <span className="text-[10px] text-emerald-400 flex items-center gap-1">
            <Check className="w-3 h-3" /> built-in
          </span>
        ) : (
          <button className="btn-secondary text-xs flex items-center gap-1" onClick={onConnect}>
            <Plus className="w-3 h-3" /> Connect
          </button>
        )}
      </div>
    </div>
  );
}

// LLM secret field → the provider it belongs to, so the single LLM connector
// renders one editable row per provider key you've added.
const LLM_KEY_TO_PROVIDER: Record<string, string> = {
  anthropic_api_key: "anthropic",
  openai_api_key: "openai",
  vertex_api_key: "vertex_gemini",
  llm_gateway_api_key: "gateway",
  groq_api_key: "groq",
  openrouter_api_key: "openrouter",
};

type EditTarget = { provider?: string; instanceId: string; values: Record<string, string> } | null;

function ConnectorCard({
  spec,
  configured,
  secretsWritable,
  isEditing,
  onEdit,
  onSaved,
  onDeleted,
  prefill,
  writableScopes,
  sharedBy,
}: {
  spec: SettingsConnectorSpec;
  configured: SettingsConnector[];
  secretsWritable: boolean;
  isEditing: boolean;
  onEdit: () => void;
  onSaved: () => void;
  onDeleted: () => void;
  prefill?: { provider?: string; instanceId?: string; values?: Record<string, string> };
  writableScopes?: WritableScope[];
  sharedBy?: SharedConnector;
}) {
  const confirm = useConfirm();
  const [editTarget, setEditTarget] = useState<EditTarget>(null);

  // Build the list of rows to show. For LLM, expand the single connector into
  // one row per provider that has a key set (so "2 secrets" reads as the two
  // providers you actually added). For everything else, one row per instance.
  const rows: {
    key: string;
    scope: string;
    scopeId: string;
    instanceId: string;
    label: string;
    badge: string;
    isPrimary: boolean;
    provider?: string;
    prefs: Record<string, string>;
    onRemove: () => Promise<void>;
  }[] = [];

  for (const c of configured) {
    if (spec.key === "llm") {
      const providersWithKeys = c.secrets_set
        .map((k) => ({ field: k, provider: LLM_KEY_TO_PROVIDER[k] }))
        .filter((p) => p.provider);
      const list = providersWithKeys.length
        ? providersWithKeys
        : [{ field: "", provider: c.provider }];
      for (const p of list) {
        rows.push({
          key: `${c.instance_id}:${p.provider}`,
          scope: c.scope,
          scopeId: c.scope_id,
          instanceId: c.instance_id,
          label: p.provider || c.provider || "llm",
          badge: p.field ? "key set" : "configured",
          isPrimary: (c.provider || "") === p.provider,
          provider: p.provider,
          prefs: c.prefs as Record<string, string>,
          onRemove: async () => {
            if (p.field) {
              await api.clearSecret(c.scope, c.scope_id, "llm", p.field, c.instance_id);
            } else {
              await api.deleteConnector(c.scope, c.scope_id, c.connector_key, c.instance_id);
            }
            onDeleted();
          },
        });
      }
    } else {
      rows.push({
        key: `${c.scope}:${c.scope_id}:${c.instance_id}`,
        scope: c.scope,
        scopeId: c.scope_id,
        instanceId: c.instance_id,
        label: c.provider || c.instance_id,
        badge: c.secrets_set.length ? `${c.secrets_set.length} secret(s) set` : "configured",
        isPrimary: false,
        provider: c.provider,
        prefs: c.prefs as Record<string, string>,
        onRemove: async () => {
          await api.deleteConnector(c.scope, c.scope_id, c.connector_key, c.instance_id);
          onDeleted();
        },
      });
    }
  }

  const openAdd = () => {
    setEditTarget(null);
    onEdit();
  };

  return (
    <div className="panel p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-indigo-400" />
            <h2 className="text-[var(--ink-50)] font-medium">{spec.label}</h2>
            {rows.length > 0 && <span className="pill text-xs">{rows.length} configured</span>}
          </div>
          <p className="text-sm text-[var(--ink-300)] mt-1">{spec.description}</p>
        </div>
        <button className="btn-secondary text-sm flex items-center gap-1.5 shrink-0" onClick={openAdd}>
          <Plus className="w-3.5 h-3.5" /> {spec.multi || spec.key === "llm" ? "Add" : "Configure"}
        </button>
      </div>

      {sharedBy && (
        <div className="mt-3 text-xs rounded-md px-3 py-2 bg-[var(--surface-raised)] border border-sky-500/30 text-[var(--ink-200)] flex items-center gap-2">
          <Check className="w-3.5 h-3.5 text-sky-400 shrink-0" />
          <span>
            Provided by your {sharedBy.scope === "org" ? "org" : "team"}{" "}
            <span className="text-[var(--ink-100)]">{sharedBy.scope_id}</span>
            {sharedBy.provider ? ` (${sharedBy.provider})` : ""} — you inherit it automatically. Add your own only to
            override it just for you.
          </span>
        </div>
      )}

      {rows.length > 0 && (
        <div className="mt-4 space-y-2">
          {rows.map((r) => (
            <div
              key={r.key}
              className="flex items-center justify-between text-sm rounded-md px-3 py-2 bg-[var(--surface-raised)]"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className="pill text-xs capitalize">{r.scope}</span>
                <span className="text-[var(--ink-100)] truncate">{r.label}</span>
                {r.isPrimary && <span className="pill text-[10px] text-sky-400">primary</span>}
                <span className="text-xs text-emerald-400 flex items-center gap-1 shrink-0">
                  <Check className="w-3 h-3" /> {r.badge}
                </span>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button
                  className="text-[var(--ink-400)] hover:text-indigo-300 p-1"
                  title="Edit / update"
                  aria-label={`Edit ${spec.label} ${r.label}`}
                  onClick={() => {
                    onEdit(); // ensure this card's form area is the open one
                    setEditTarget({ provider: r.provider, instanceId: r.instanceId, values: { ...r.prefs } });
                  }}
                >
                  <Pencil className="w-3.5 h-3.5" />
                </button>
                <button
                  className="text-[var(--ink-400)] hover:text-red-400 p-1"
                  title="Remove"
                  aria-label={`Remove ${spec.label} ${r.label}`}
                  onClick={async () => {
                    const ok = await confirm({
                      title: `Remove ${r.label}?`,
                      message:
                        spec.key === "llm"
                          ? "This deletes this provider's stored key. Your other providers stay."
                          : "This removes the connector and its stored credentials. This can't be undone.",
                      confirmLabel: "Remove",
                      tone: "danger",
                    });
                    if (!ok) return;
                    await r.onRemove();
                  }}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {(isEditing || editTarget) && (
        <ConnectorForm
          spec={spec}
          secretsWritable={secretsWritable}
          onSaved={() => {
            setEditTarget(null);
            onSaved();
          }}
          prefill={editTarget ?? prefill}
          writableScopes={writableScopes}
          sharedBy={sharedBy}
        />
      )}
    </div>
  );
}

function ConnectorForm({
  spec,
  secretsWritable,
  onSaved,
  prefill,
  writableScopes,
  sharedBy,
}: {
  spec: SettingsConnectorSpec;
  secretsWritable: boolean;
  onSaved: () => void;
  // Pre-populate the form (e.g. "Connect" from the MCP marketplace).
  prefill?: { provider?: string; instanceId?: string; values?: Record<string, string> };
  writableScopes?: WritableScope[];
  sharedBy?: SharedConnector;
}) {
  const confirm = useConfirm();
  const scopeOptions = writableScopes && writableScopes.length ? writableScopes : [{ scope: "user", scope_id: "", label: "Just me" }];
  const [scope, setScope] = useState<string>(scopeOptions[0]?.scope || "user");
  const [scopeId, setScopeId] = useState(scopeOptions[0]?.scope_id || "");
  const [provider, setProvider] = useState(prefill?.provider || spec.providers[0] || "");
  const [instanceId, setInstanceId] = useState(prefill?.instanceId || "default");
  const [values, setValues] = useState<Record<string, string>>(prefill?.values || {});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const secretKeys = useMemo(
    () => new Set(spec.fields.filter((f) => f.secret).map((f) => f.key)),
    [spec.fields]
  );

  // Show only the fields for the selected provider (untagged fields show
  // for every provider). Keeps multi-provider connectors like Observability
  // from dumping all 7 vendors' fields at once.
  const visibleFields = useMemo(
    () => spec.fields.filter((f) => !f.provider || f.provider === provider),
    [spec.fields, provider]
  );

  // Live model suggestions for LLM connectors: ask the backend which models
  // this provider serves (evaluated against the caller's own keys) and offer
  // them as a datalist on *_model fields — free text stays allowed so
  // gateway aliases / brand-new models still work.
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  // Per-model enable/disable: null = no policy (everything enabled);
  // a Set = only those models allowed. Saved as prefs.enabled_models.
  const [disabledModels, setDisabledModels] = useState<Set<string>>(new Set());
  // Optional same-provider fallback model. Saved as prefs.fallback_model.
  const [fallbackModel, setFallbackModel] = useState("");
  useEffect(() => {
    if (spec.key !== "llm" || !provider) return;
    let cancelled = false;
    api
      .listProviderModels(provider)
      .then((r) => {
        if (cancelled) return;
        setModelOptions(r.models.map((m) => m.id));
        setDisabledModels(new Set(r.models.filter((m) => m.enabled === false).map((m) => m.id)));
      })
      .catch(() => setModelOptions([]));
    return () => {
      cancelled = true;
    };
  }, [spec.key, provider]);

  const toggleModel = (id: string) => {
    setDisabledModels((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const save = async () => {
    // Override guard: saving at user scope when the org/team already provides
    // this connector — confirm, don't block (user scope wins by design).
    if (scope === "user" && sharedBy) {
      const where = sharedBy.scope === "org" ? `org ${sharedBy.scope_id}` : `team ${sharedBy.scope_id}`;
      const ok = await confirm({
        title: `Your ${where} already provides ${spec.label}`,
        message:
          `You inherit it automatically — you don't need your own. Saving here overrides it ` +
          `just for you. Proceed with a personal override?`,
        confirmLabel: "Override for me",
      });
      if (!ok) return;
    }
    setSaving(true);
    setErr(null);
    try {
      const prefs: Record<string, string> = {};
      const secrets: Record<string, string> = {};
      const visibleKeys = new Set(visibleFields.map((f) => f.key));
      for (const [k, v] of Object.entries(values)) {
        if (!v || !visibleKeys.has(k)) continue; // only the selected provider's fields
        if (secretKeys.has(k)) secrets[k] = v;
        else prefs[k] = v;
      }
      // Model policy: persist the ENABLED set only when something is
      // disabled — no policy means every model stays available.
      if (spec.key === "llm" && disabledModels.size > 0 && modelOptions.length > 0) {
        prefs["enabled_models"] = modelOptions.filter((m) => !disabledModels.has(m)).join(",");
      }
      if (spec.key === "llm" && fallbackModel) {
        prefs["fallback_model"] = fallbackModel;
      }
      await api.saveConnector({
        scope,
        scope_id: scope === "global" ? "" : scopeId,
        connector_key: spec.key,
        provider,
        instance_id: spec.multi ? instanceId : "default",
        prefs,
        secrets,
      });
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-4 pt-4 border-t border-[var(--surface-border)] space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-[var(--ink-300)]">Apply to</span>
          <select
            className="field mt-1 w-full"
            value={`${scope}:${scopeId}`}
            onChange={(e) => {
              const opt = scopeOptions.find((s) => `${s.scope}:${s.scope_id}` === e.target.value);
              if (opt) {
                setScope(opt.scope);
                setScopeId(opt.scope_id);
              }
            }}
          >
            {scopeOptions.map((s) => (
              <option key={`${s.scope}:${s.scope_id}`} value={`${s.scope}:${s.scope_id}`}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-[var(--ink-300)]">Provider</span>
          <select
            className="field mt-1 w-full"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            {spec.providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        {spec.multi && (
          <label className="block">
            <span className="text-xs text-[var(--ink-300)]">Instance name</span>
            <input
              className="field mt-1 w-full"
              value={instanceId}
              onChange={(e) => setInstanceId(e.target.value)}
              placeholder="e.g. my-tools"
            />
          </label>
        )}
      </div>

      <div className="space-y-3">
        {visibleFields.map((f) => {
          const isModelField =
            spec.key === "llm" && f.key.endsWith("_model") && modelOptions.length > 0;
          return (
            <label key={f.key} className="block">
              <span className="text-xs text-[var(--ink-300)] flex items-center gap-1.5">
                {isModelField ? "Primary Model" : f.label}
                {f.secret && <KeyRound className="w-3 h-3 text-amber-400" />}
                {(f.required || isModelField) && <span className="text-red-400">*</span>}
              </span>
              {isModelField ? (
                // Pick from the discovered list — no typing. First enabled
                // model is the default primary.
                <select
                  className="field mt-1 w-full"
                  value={values[f.key] || ""}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                >
                  <option value="">Select a model…</option>
                  {modelOptions
                    .filter((m) => !disabledModels.has(m))
                    .map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                </select>
              ) : (
                <input
                  className="field mt-1 w-full"
                  type={f.secret ? "password" : "text"}
                  autoComplete={f.secret ? "new-password" : "off"}
                  disabled={f.secret && !secretsWritable}
                  placeholder={
                    f.secret && !secretsWritable ? "secret storage read-only" : f.placeholder
                  }
                  value={values[f.key] || ""}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                />
              )}
              {f.help && !isModelField && (
                <span className="text-xs text-[var(--ink-400)] mt-0.5 block">{f.help}</span>
              )}
            </label>
          );
        })}

        {/* Fallback model — picked from the same list; used when the primary
            errors before the chain moves to the next provider. */}
        {spec.key === "llm" && modelOptions.length > 0 && (
          <label className="block">
            <span className="text-xs text-[var(--ink-300)]">Fallback Model</span>
            <select
              className="field mt-1 w-full"
              value={fallbackModel}
              onChange={(e) => setFallbackModel(e.target.value)}
            >
              <option value="">None — fall through to the next provider</option>
              {modelOptions
                .filter((m) => !disabledModels.has(m))
                .map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
            </select>
            <span className="text-xs text-[var(--ink-400)] mt-0.5 block">
              Tried on the same provider if the primary model fails.
            </span>
          </label>
        )}
      </div>

      {spec.key === "llm" && modelOptions.length > 0 && (
        <div>
          <span className="text-xs text-[var(--ink-300)]">
            Available models — click to enable/disable for your account
          </span>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {modelOptions.map((m) => {
              const off = disabledModels.has(m);
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => toggleModel(m)}
                  className={`px-2 py-0.5 rounded-full text-xs border transition-colors ${
                    off
                      ? "border-[var(--surface-border)] text-[var(--ink-400)] line-through opacity-60"
                      : "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
                  }`}
                  title={off ? "Disabled — click to enable" : "Enabled — click to disable"}
                >
                  {m}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {err && <p className="text-sm text-red-300">{err}</p>}

      <div className="flex justify-end">
        <button className="btn-primary text-sm flex items-center gap-1.5" disabled={saving} onClick={save}>
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          Save connector
        </button>
      </div>
    </div>
  );
}
