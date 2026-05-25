"use client";

import { useEffect, useState } from "react";
import { PackageOpen, RefreshCw } from "lucide-react";

type Counts = {
  skills: number;
  prompts: number;
  mcp_servers: number;
  agents: number;
};

type Health = {
  reachable: boolean;
  status?: string;
  error?: string;
};

type Skill = { name: string; description: string; version: string; category: string; title: string };
type Prompt = { name: string; description: string; version: string };
type McpServer = { name: string; description: string; version: string; type: string; url: string };
type Agent = {
  name: string;
  description: string;
  version: string;
  framework: string;
  language: string;
  model_provider: string;
  model_name: string;
};

type Tab = "skills" | "prompts" | "mcp-servers" | "agents";

export default function RegistryPage() {
  const [counts, setCounts] = useState<Counts | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>("skills");
  const [items, setItems] = useState<Skill[] | Prompt[] | McpServer[] | Agent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(active: Tab) {
    setLoading(true);
    setError(null);
    try {
      const [hRes, cRes, iRes] = await Promise.all([
        fetch("/api/registry/health"),
        fetch("/api/registry/counts").catch(() => null),
        fetch(`/api/registry/${active}`),
      ]);
      const h: Health = await hRes.json();
      setHealth(h);
      if (cRes && cRes.ok) {
        setCounts(await cRes.json());
      }
      if (iRes.ok) {
        setItems(await iRes.json());
      } else {
        const body = await iRes.text();
        setError(`/${active}: HTTP ${iRes.status} — ${body.slice(0, 160)}`);
        setItems([]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(tab);
  }, [tab]);

  async function refresh() {
    await fetch("/api/registry/refresh", { method: "POST" }).catch(() => null);
    await load(tab);
  }

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Catalog</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <PackageOpen className="w-5 h-5 text-indigo-400" /> Agent Registry
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Catalogue browser — backed by aregistry, augmented with local YAML.
          </p>
          {health && (
            <p className="text-xs mt-2 flex items-center gap-2 font-mono">
              <span className={`dot ${health.reachable ? "dot-ok" : "dot-error"}`} />
              <span className={health.reachable ? "text-emerald-400" : "text-red-400"}>
                {health.reachable ? "reachable" : "unreachable"}
              </span>
              {health.error && <span className="text-[var(--ink-500)]">· {health.error}</span>}
            </p>
          )}
        </div>
        <button
          onClick={refresh}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-[var(--surface-border)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] text-[var(--ink-100)] transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </button>
      </header>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <CountCard label="Skills" value={counts?.skills} active={tab === "skills"} onClick={() => setTab("skills")} />
        <CountCard label="Prompts" value={counts?.prompts} active={tab === "prompts"} onClick={() => setTab("prompts")} />
        <CountCard
          label="MCP Servers"
          value={counts?.mcp_servers}
          active={tab === "mcp-servers"}
          onClick={() => setTab("mcp-servers")}
        />
        <CountCard label="Agents" value={counts?.agents} active={tab === "agents"} onClick={() => setTab("agents")} />
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      <section className="panel overflow-hidden">
        {loading ? (
          <div className="p-6 text-sm text-[var(--ink-500)]">Loading…</div>
        ) : items.length === 0 ? (
          <div className="p-6 text-sm text-[var(--ink-500)]">No entries.</div>
        ) : (
          renderTable(tab, items)
        )}
      </section>
    </div>
  );
}

function CountCard({ label, value, active, onClick }: { label: string; value?: number; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`text-left rounded-lg border px-4 py-3 transition-colors ${
        active
          ? "border-indigo-500/50 bg-indigo-500/10 ring-1 ring-inset ring-indigo-500/20"
          : "border-[var(--surface-border)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] hover:border-[var(--surface-border-strong)]"
      }`}
    >
      <div className="label-eyebrow">{label}</div>
      <div className="text-2xl font-mono font-medium text-[var(--ink-50)] mt-1 tabular-nums">{value ?? "—"}</div>
    </button>
  );
}

function renderTable(tab: Tab, items: Skill[] | Prompt[] | McpServer[] | Agent[]) {
  if (tab === "skills") {
    const rows = items as Skill[];
    return (
      <table className="min-w-full text-sm">
        <thead className="bg-[var(--surface-2)] text-left label-eyebrow">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Category</th><th className="px-4 py-2">Version</th><th className="px-4 py-2">Title</th></tr>
        </thead>
        <tbody className="divide-y divide-[var(--surface-border)]">
          {rows.map((s) => (
            <tr key={s.name}>
              <td className="px-4 py-2 font-mono text-[var(--ink-100)]">{s.name}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{s.category}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{s.version}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{s.title}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (tab === "prompts") {
    const rows = items as Prompt[];
    return (
      <table className="min-w-full text-sm">
        <thead className="bg-[var(--surface-2)] text-left label-eyebrow">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Version</th><th className="px-4 py-2">Description</th></tr>
        </thead>
        <tbody className="divide-y divide-[var(--surface-border)]">
          {rows.map((p) => (
            <tr key={p.name}>
              <td className="px-4 py-2 font-mono text-[var(--ink-100)]">{p.name}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{p.version}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{p.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (tab === "mcp-servers") {
    const rows = items as McpServer[];
    return (
      <table className="min-w-full text-sm">
        <thead className="bg-[var(--surface-2)] text-left label-eyebrow">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Type</th><th className="px-4 py-2">URL</th><th className="px-4 py-2">Version</th></tr>
        </thead>
        <tbody className="divide-y divide-[var(--surface-border)]">
          {rows.map((m) => (
            <tr key={m.name}>
              <td className="px-4 py-2 font-mono text-[var(--ink-100)]">{m.name}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{m.type}</td>
              <td className="px-4 py-2 text-[var(--ink-300)] font-mono text-xs">{m.url || "—"}</td>
              <td className="px-4 py-2 text-[var(--ink-300)]">{m.version}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  const rows = items as Agent[];
  return (
    <table className="min-w-full text-sm">
      <thead className="bg-[var(--surface-2)] text-left label-eyebrow">
        <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Framework</th><th className="px-4 py-2">Model</th><th className="px-4 py-2">Description</th></tr>
      </thead>
      <tbody className="divide-y divide-[var(--surface-border)]">
        {rows.map((a) => (
          <tr key={a.name}>
            <td className="px-4 py-2 font-mono text-[var(--ink-100)]">{a.name}</td>
            <td className="px-4 py-2 text-[var(--ink-300)]">{a.framework}</td>
            <td className="px-4 py-2 text-[var(--ink-300)] font-mono text-xs">{a.model_provider}/{a.model_name}</td>
            <td className="px-4 py-2 text-[var(--ink-300)]">{a.description}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
