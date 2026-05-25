"use client";

import { useEffect, useState } from "react";
import { Users } from "lucide-react";

type Agent = {
  name: string;
  description: string;
  version: string;
  framework: string;
  language: string;
  model_provider: string;
  model_name: string;
  skills: string[];
  prompts: string[];
};

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    fetch("/api/registry/agents")
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
        return r.json();
      })
      .then((data: Agent[]) => setAgents(data))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const filtered = query
    ? agents.filter((a) => a.name.toLowerCase().includes(query.toLowerCase()))
    : agents;

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="label-eyebrow">Catalog</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <Users className="w-5 h-5 text-indigo-400" /> Agents
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Agents catalogued in aregistry. Each card shows the model + framework binding the runtime will instantiate.
          </p>
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by name…"
          className="px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50"
        />
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="text-sm text-[var(--ink-500)]">
          {agents.length === 0
            ? "Registry returned 0 agents. Make sure the registry-bootstrap Job has completed."
            : `No agents match "${query}".`}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {filtered.map((a) => (
            <article
              key={a.name}
              className="panel p-4 hover:border-[var(--surface-border-strong)] transition-colors"
            >
              <div className="flex items-baseline justify-between gap-2">
                <h2 className="text-sm font-mono font-semibold text-[var(--ink-50)]">{a.name}</h2>
                <span className="text-[11px] font-mono text-[var(--ink-500)]">v{a.version}</span>
              </div>
              <p className="text-sm text-[var(--ink-300)] mt-1">{a.description}</p>
              <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="label-eyebrow">Model</dt>
                <dd className="font-mono text-[var(--ink-100)]">{a.model_provider}/{a.model_name}</dd>
                <dt className="label-eyebrow">Framework</dt>
                <dd className="text-[var(--ink-100)]">{a.framework}</dd>
                <dt className="label-eyebrow">Language</dt>
                <dd className="text-[var(--ink-100)]">{a.language}</dd>
              </dl>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
