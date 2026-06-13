"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ChevronDown, KeyRound, Loader2, Plus, Save, Store, Trash2 } from "lucide-react";
import { useConfirm } from "@/components/confirm-dialog";
import {
  api,
  type McpMarketplaceEntry,
  type SettingsCatalog,
  type SettingsConnector,
  type SettingsConnectorSpec,
} from "@/lib/api";

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

const SCOPES = ["user", "team", "tenant", "global"] as const;

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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cat, mine] = await Promise.all([api.getSettingsCatalog(), api.listSettings()]);
      setCatalog(cat);
      setConnectors(mine.connectors);
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

  const secretsWritable = catalog?.secrets_writable ?? false;

  return (
    <div className="p-7 w-full">
      <div className="label-eyebrow">Mission control</div>
      <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1">Settings</h1>
      <p className="text-sm text-[var(--ink-300)] mt-1">
        Connect your own LLM, source control, memory, Slack and MCP servers. Your credentials drive
        both your conversations and your pipeline runs.
      </p>

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

      {loading ? (
        <div className="mt-8 flex items-center gap-2 text-[var(--ink-300)]">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading…
        </div>
      ) : (
        <div className="mt-6 space-y-4">
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
              />
            );
          })}

          <McpMarketplace
            onConnect={(entry) => {
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

  return (
    <div className="panel p-5">
      <button className="flex items-center justify-between w-full gap-4" onClick={() => setOpen((v) => !v)}>
        <div className="text-left">
          <div className="flex items-center gap-2">
            <Store className="w-4 h-4 text-indigo-400" />
            <h2 className="text-[var(--ink-50)] font-medium">MCP Marketplace</h2>
            {data && <span className="pill text-xs">{connectable.length + builtin.length} servers</span>}
          </div>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Browse every MCP server in the registry and connect your own — tools they expose become
            available to your agents and chat.
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

          {!loading && connectable.length > 0 && (
            <>
              <div className="label-eyebrow mb-2">Connect your own</div>
              <div className="grid sm:grid-cols-2 gap-3">
                {connectable.map((e) => (
                  <McpCard key={e.name} entry={e} onConnect={() => onConnect(e)} />
                ))}
              </div>
            </>
          )}

          {!loading && builtin.length > 0 && (
            <>
              <div className="label-eyebrow mb-2 mt-5">Built-in (always on)</div>
              <div className="grid sm:grid-cols-2 gap-3">
                {builtin.map((e) => (
                  <McpCard key={e.name} entry={e} builtin />
                ))}
              </div>
            </>
          )}

          {!loading && connectable.length === 0 && builtin.length === 0 && (
            <p className="text-sm text-[var(--ink-400)]">No MCP servers in the registry yet.</p>
          )}
        </div>
      )}
    </div>
  );
}

function McpCard({
  entry,
  onConnect,
  builtin,
}: {
  entry: McpMarketplaceEntry;
  onConnect?: () => void;
  builtin?: boolean;
}) {
  return (
    <div className="rounded-md border border-[var(--surface-border)] bg-[var(--surface-raised)] p-3 flex flex-col">
      <div className="flex items-start justify-between gap-2">
        <div className="text-[var(--ink-100)] text-sm font-medium">{entry.display_name || entry.name}</div>
        {entry.category && <span className="pill text-[10px] capitalize">{entry.category}</span>}
      </div>
      <p className="text-xs text-[var(--ink-300)] mt-1 line-clamp-3 flex-1">{entry.description}</p>
      <div className="flex items-center justify-between mt-2">
        <span className="text-[10px] text-[var(--ink-400)]">
          {entry.tool_count} tool{entry.tool_count === 1 ? "" : "s"} · {entry.auth_mode}
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

function ConnectorCard({
  spec,
  configured,
  secretsWritable,
  isEditing,
  onEdit,
  onSaved,
  onDeleted,
  prefill,
}: {
  spec: SettingsConnectorSpec;
  configured: SettingsConnector[];
  secretsWritable: boolean;
  isEditing: boolean;
  onEdit: () => void;
  onSaved: () => void;
  onDeleted: () => void;
  prefill?: { provider?: string; instanceId?: string; values?: Record<string, string> };
}) {
  const confirm = useConfirm();
  return (
    <div className="panel p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-indigo-400" />
            <h2 className="text-[var(--ink-50)] font-medium">{spec.label}</h2>
            {configured.length > 0 && (
              <span className="pill text-xs">{configured.length} configured</span>
            )}
          </div>
          <p className="text-sm text-[var(--ink-300)] mt-1">{spec.description}</p>
        </div>
        <button className="btn-secondary text-sm flex items-center gap-1.5 shrink-0" onClick={onEdit}>
          <Plus className="w-3.5 h-3.5" /> {spec.multi ? "Add" : "Configure"}
        </button>
      </div>

      {configured.length > 0 && (
        <div className="mt-4 space-y-2">
          {configured.map((c) => (
            <div
              key={`${c.scope}:${c.scope_id}:${c.instance_id}`}
              className="flex items-center justify-between text-sm rounded-md px-3 py-2 bg-[var(--surface-raised)]"
            >
              <div className="flex items-center gap-3">
                <span className="pill text-xs capitalize">{c.scope}</span>
                <span className="text-[var(--ink-100)]">{c.provider || c.instance_id}</span>
                {c.secrets_set.length > 0 && (
                  <span className="text-xs text-emerald-400 flex items-center gap-1">
                    <Check className="w-3 h-3" /> {c.secrets_set.length} secret(s) set
                  </span>
                )}
              </div>
              <button
                className="text-[var(--ink-400)] hover:text-red-400"
                title="Delete"
                aria-label={`Delete ${spec.label} (${c.scope})`}
                onClick={async () => {
                  const ok = await confirm({
                    title: `Delete ${spec.label} connector?`,
                    message: "This removes the connector and its stored credentials. This can't be undone.",
                    confirmLabel: "Delete",
                    tone: "danger",
                  });
                  if (!ok) return;
                  await api.deleteConnector(c.scope, c.scope_id, c.connector_key, c.instance_id);
                  onDeleted();
                }}
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
      )}

      {isEditing && (
        <ConnectorForm
          spec={spec}
          secretsWritable={secretsWritable}
          onSaved={onSaved}
          prefill={prefill}
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
}: {
  spec: SettingsConnectorSpec;
  secretsWritable: boolean;
  onSaved: () => void;
  // Pre-populate the form (e.g. "Connect" from the MCP marketplace).
  prefill?: { provider?: string; instanceId?: string; values?: Record<string, string> };
}) {
  const [scope, setScope] = useState<string>("user");
  const [scopeId, setScopeId] = useState("");
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
          <span className="text-xs text-[var(--ink-300)]">Scope</span>
          <select className="field mt-1 w-full" value={scope} onChange={(e) => setScope(e.target.value)}>
            {SCOPES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        {scope !== "global" && scope !== "user" && (
          <label className="block">
            <span className="text-xs text-[var(--ink-300)]">{scope} id</span>
            <input
              className="field mt-1 w-full"
              value={scopeId}
              onChange={(e) => setScopeId(e.target.value)}
              placeholder={`${scope} id`}
            />
          </label>
        )}
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
