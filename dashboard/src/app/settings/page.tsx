"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, KeyRound, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { useConfirm } from "@/components/confirm-dialog";
import {
  api,
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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cat, mine] = await Promise.all([api.getSettingsCatalog(), api.listSettings()]);
      setCatalog(cat);
      setConnectors(mine.connectors);
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
                onEdit={() => setEditing(editing === spec.key ? null : spec.key)}
                onSaved={() => {
                  setEditing(null);
                  void load();
                }}
                onDeleted={() => void load()}
              />
            );
          })}
        </div>
      )}
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
}: {
  spec: SettingsConnectorSpec;
  configured: SettingsConnector[];
  secretsWritable: boolean;
  isEditing: boolean;
  onEdit: () => void;
  onSaved: () => void;
  onDeleted: () => void;
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
        />
      )}
    </div>
  );
}

function ConnectorForm({
  spec,
  secretsWritable,
  onSaved,
}: {
  spec: SettingsConnectorSpec;
  secretsWritable: boolean;
  onSaved: () => void;
}) {
  const [scope, setScope] = useState<string>("user");
  const [scopeId, setScopeId] = useState("");
  const [provider, setProvider] = useState(spec.providers[0] || "");
  const [instanceId, setInstanceId] = useState("default");
  const [values, setValues] = useState<Record<string, string>>({});
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
        {visibleFields.map((f) => (
          <label key={f.key} className="block">
            <span className="text-xs text-[var(--ink-300)] flex items-center gap-1.5">
              {f.label}
              {f.secret && <KeyRound className="w-3 h-3 text-amber-400" />}
              {f.required && <span className="text-red-400">*</span>}
            </span>
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
            {f.help && <span className="text-xs text-[var(--ink-400)] mt-0.5 block">{f.help}</span>}
          </label>
        ))}
      </div>

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
