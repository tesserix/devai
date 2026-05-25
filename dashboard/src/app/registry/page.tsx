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
    <div className="p-6 space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <PackageOpen className="w-5 h-5" /> Agent Registry
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Catalogue browser — backed by aregistry, augmented with local YAML.
          </p>
          {health && (
            <p className="text-xs mt-2">
              Status:{" "}
              <span className={health.reachable ? "text-emerald-600" : "text-red-600"}>
                {health.reachable ? "reachable" : "unreachable"}
              </span>
              {health.error && <span className="text-gray-400 ml-2">— {health.error}</span>}
            </p>
          )}
        </div>
        <button
          onClick={refresh}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700"
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
        <div className="rounded-md border border-red-200 bg-red-50 dark:bg-red-900/20 dark:border-red-800 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      <section className="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 overflow-hidden">
        {loading ? (
          <div className="p-6 text-sm text-gray-500 dark:text-gray-400">Loading…</div>
        ) : items.length === 0 ? (
          <div className="p-6 text-sm text-gray-500 dark:text-gray-400">No entries.</div>
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
          ? "border-indigo-300 bg-indigo-50 dark:border-indigo-600 dark:bg-indigo-900/30"
          : "border-gray-200 bg-white hover:bg-gray-50 dark:border-gray-800 dark:bg-gray-900 dark:hover:bg-gray-800"
      }`}
    >
      <div className="text-[11px] uppercase tracking-wider text-gray-500 dark:text-gray-400">{label}</div>
      <div className="text-2xl font-semibold text-gray-900 dark:text-gray-100 mt-1">{value ?? "—"}</div>
    </button>
  );
}

function renderTable(tab: Tab, items: Skill[] | Prompt[] | McpServer[] | Agent[]) {
  if (tab === "skills") {
    const rows = items as Skill[];
    return (
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-800/40 text-left text-xs uppercase tracking-wider text-gray-500">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Category</th><th className="px-4 py-2">Version</th><th className="px-4 py-2">Title</th></tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.map((s) => (
            <tr key={s.name}>
              <td className="px-4 py-2 font-mono text-gray-900 dark:text-gray-100">{s.name}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{s.category}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{s.version}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{s.title}</td>
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
        <thead className="bg-gray-50 dark:bg-gray-800/40 text-left text-xs uppercase tracking-wider text-gray-500">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Version</th><th className="px-4 py-2">Description</th></tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.map((p) => (
            <tr key={p.name}>
              <td className="px-4 py-2 font-mono text-gray-900 dark:text-gray-100">{p.name}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{p.version}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{p.description}</td>
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
        <thead className="bg-gray-50 dark:bg-gray-800/40 text-left text-xs uppercase tracking-wider text-gray-500">
          <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Type</th><th className="px-4 py-2">URL</th><th className="px-4 py-2">Version</th></tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.map((m) => (
            <tr key={m.name}>
              <td className="px-4 py-2 font-mono text-gray-900 dark:text-gray-100">{m.name}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{m.type}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400 font-mono text-xs">{m.url || "—"}</td>
              <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{m.version}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  const rows = items as Agent[];
  return (
    <table className="min-w-full text-sm">
      <thead className="bg-gray-50 dark:bg-gray-800/40 text-left text-xs uppercase tracking-wider text-gray-500">
        <tr><th className="px-4 py-2">Name</th><th className="px-4 py-2">Framework</th><th className="px-4 py-2">Model</th><th className="px-4 py-2">Description</th></tr>
      </thead>
      <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
        {rows.map((a) => (
          <tr key={a.name}>
            <td className="px-4 py-2 font-mono text-gray-900 dark:text-gray-100">{a.name}</td>
            <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{a.framework}</td>
            <td className="px-4 py-2 text-gray-600 dark:text-gray-400 font-mono text-xs">{a.model_provider}/{a.model_name}</td>
            <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{a.description}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
