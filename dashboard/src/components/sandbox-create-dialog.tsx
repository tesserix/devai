"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot } from "lucide-react";
import { ModelPicker } from "@/components/model-picker";
import { Select } from "@/components/ui/select";

type RegistryAgent = {
  name: string;
  version: string;
  description?: string;
  model_provider: string;
  model_name: string;
};

const TOOL_MODES = [
  { value: "mock", label: "Mock", description: "Canned responses — nothing outside is touched." },
  { value: "replay", label: "Replay", description: "Recorded responses from a previous run." },
  { value: "block", label: "Block", description: "Refuse every tool call." },
  { value: "real", label: "Real", description: "Calls reach the actual system." },
];

export function SandboxCreateDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [agents, setAgents] = useState<RegistryAgent[]>([]);
  const [versions, setVersions] = useState<string[]>([]);
  const [agent, setAgent] = useState("");
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("claude-sonnet-4-20250514");
  const [adkVersion, setAdkVersion] = useState("");
  const [toolMode, setToolMode] = useState("mock");
  const [ttlHours, setTtlHours] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    fetch("/api/registry/agents", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((data: RegistryAgent[]) => {
        setAgents(data);
        if (data.length > 0) setAgent((a) => a || data[0].name);
      })
      .catch(() => setAgents([]));
    // The picker offers the latest few runtime releases; the first is the default.
    fetch("/api/adk/versions", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { versions: [] }))
      .then((d: { versions: string[]; default?: string }) => {
        setVersions(d.versions ?? []);
        setAdkVersion((v) => v || d.default || d.versions?.[0] || "");
      })
      .catch(() => setVersions([]));
  }, [open]);

  // Pinning an agent pre-fills the model it was published with — still editable,
  // because comparing the same agent across models is the point of a sandbox.
  useEffect(() => {
    const chosen = agents.find((a) => a.name === agent);
    if (!chosen) return;
    if (chosen.model_provider) setProvider(chosen.model_provider);
    if (chosen.model_name) setModel(chosen.model_name);
  }, [agent, agents]);

  const agentOptions = useMemo(
    () =>
      agents.map((a) => ({
        value: a.name,
        label: a.name,
        description: a.description,
        badge: a.version || undefined,
      })),
    [agents],
  );

  if (!open) return null;

  const selected = agents.find((a) => a.name === agent);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/sandboxes", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent: { name: agent, version: selected?.version || "latest" },
          model: { provider, model },
          adk_version: adkVersion || null,
          tools: { default_mode: toolMode },
          ttl_seconds: Math.round(ttlHours * 3600),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      onCreated(body.id as string);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const field = "w-full px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] focus:outline-none focus:border-indigo-500/50";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4"
      style={{ background: "var(--surface-overlay)", backdropFilter: "blur(4px)" }}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="rounded-xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto"
        style={{
          background: "var(--surface-raised)",
          border: "1px solid var(--border)",
          boxShadow: "var(--shadow-raised)",
        }}
      >
        <h2 className="font-serif text-lg font-medium" style={{ color: "var(--ink-strong)" }}>
          New sandbox
        </h2>
        <p className="text-sm mt-1 mb-5" style={{ color: "var(--ink-soft)" }}>
          Pin an agent, a model and a runtime release. Everything that could change a result is fixed
          here, so two runs are comparable.
        </p>

        <div className="space-y-4">
          <div>
            <div className="flex items-baseline justify-between">
              <span className="label-eyebrow">Agent</span>
              <a href="/agents/studio" className="text-[11px] text-indigo-300 hover:underline">
                Build a new one
              </a>
            </div>
            <Select
              value={agent}
              onChange={setAgent}
              options={agentOptions}
              mono
              searchable
              placeholder={agents.length === 0 ? "No published agents" : "Choose an agent"}
              icon={<Bot className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--ink-muted)" }} />}
              ariaLabel="Agent"
            />
          </div>

          <ModelPicker
            provider={provider}
            model={model}
            onChange={(next) => {
              setProvider(next.provider);
              setModel(next.model);
            }}
          />

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="sb-adk" className="label-eyebrow">
                Runtime (ADK)
              </label>
              <Select
                id="sb-adk"
                value={adkVersion}
                onChange={setAdkVersion}
                mono
                placeholder="default"
                options={versions.map((v, i) => ({
                  value: v,
                  label: v,
                  badge: i === 0 ? "latest" : undefined,
                }))}
                ariaLabel="Runtime version"
              />
            </div>
            <div>
              <label htmlFor="sb-ttl" className="label-eyebrow">
                Expires in (hours)
              </label>
              <input
                id="sb-ttl"
                type="number"
                min={1}
                max={24}
                value={ttlHours}
                onChange={(e) => setTtlHours(Number(e.target.value))}
                className={field}
              />
            </div>
          </div>

          <div>
            <label htmlFor="sb-tools" className="label-eyebrow">
              Tools
            </label>
            <Select id="sb-tools" value={toolMode} onChange={setToolMode} options={TOOL_MODES} ariaLabel="Tool mode" />
          </div>

          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="btn-secondary !py-1 !px-3 !text-xs">
              Cancel
            </button>
            <button
              type="button"
              onClick={create}
              disabled={busy || !agent}
              className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create sandbox"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
