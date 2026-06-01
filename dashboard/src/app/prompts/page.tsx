"use client";

import { useEffect, useState } from "react";
import { ExternalLink, MessageSquareText } from "lucide-react";

import { api, type RegistryItem } from "@/lib/api";
import { aregistryUrl } from "@/lib/aregistry";

/**
 * Prompts — reusable prompt templates in the shared registry. Agents reference
 * them by name (Create-Agent → Prompts picker). Read-only here; prompts are
 * published to the registry via `agentic apply` or an agent's composition.
 */
export default function PromptsPage() {
  const [items, setItems] = useState<RegistryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api
      .listRegistryPrompts()
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
            <MessageSquareText className="w-5 h-5 text-indigo-400" /> Prompts
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Reusable prompt templates in the registry. Compose them into agents from the Create-Agent picker.
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
        <div className="panel p-6 text-sm text-[var(--ink-300)]">No prompts in the registry yet.</div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {filtered.map((it) => (
            <a
              key={it.name}
              href={aregistryUrl("prompts", it.name)}
              target="_blank"
              rel="noreferrer"
              title="Open in the agent registry"
              className="panel p-4 block transition-colors hover:border-indigo-500/40 group"
            >
              <div className="font-mono text-sm text-[var(--ink-50)] flex items-center justify-between gap-2">
                <span className="truncate">{it.name}</span>
                <ExternalLink className="w-3.5 h-3.5 text-[var(--ink-500)] opacity-0 group-hover:opacity-100 shrink-0" />
              </div>
              {(it.title || it.display_name) && (
                <div className="text-xs text-[var(--ink-300)] mt-0.5">{it.title || it.display_name}</div>
              )}
              {it.description && (
                <p className="text-[11px] text-[var(--ink-400)] mt-2 line-clamp-2">{it.description}</p>
              )}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
