"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Plus, Wrench } from "lucide-react";

import { api, type RegistryItem } from "@/lib/api";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";

/**
 * Tools — custom MCP servers registered in the shared registry. Each is a
 * tool surface an agent can compose; once routed through agentgateway it's
 * reachable at /mcp/<name>. Author new ones via Register Tool.
 */
export default function ToolsPage() {
  const [items, setItems] = useState<RegistryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api
      .listRegistryMcpServers()
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const filtered = query
    ? items.filter((i) => i.name.toLowerCase().includes(query.toLowerCase()))
    : items;

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="label-eyebrow">Catalog</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <Wrench className="w-5 h-5 text-indigo-400" /> Tools
            <GuidanceInfo id="tools" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Custom tools registered as MCP servers. Compose them into agents from the Create-Agent picker.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name…"
            className="px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50"
          />
          <Link
            href="/tools/new"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium whitespace-nowrap"
          >
            <Plus className="w-4 h-4" /> Register Tool
          </Link>
        </div>
      </header>

      <GuidancePanel id="tools" />

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="panel p-6 text-sm text-[var(--ink-300)]">
          No tools registered yet. <Link href="/tools/new" className="text-indigo-400 hover:underline">Register one</Link>.
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {filtered.map((it) => (
            <div key={it.name} className="panel p-4">
              <div className="font-mono text-sm text-[var(--ink-50)]">{it.name}</div>
              {(it.title || it.display_name) && (
                <div className="text-xs text-[var(--ink-300)] mt-0.5">{it.title || it.display_name}</div>
              )}
              {it.description && (
                <p className="text-[11px] text-[var(--ink-400)] mt-2 line-clamp-2">{it.description}</p>
              )}
              {it.version && (
                <span className="inline-block mt-2 text-[10px] text-[var(--ink-500)] font-mono">v{String(it.version)}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
