"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Plus, Sparkles } from "lucide-react";

import { api, type RegistryItem } from "@/lib/api";

/**
 * Skills — reusable capabilities in the shared registry. Compose them into
 * agents from the Create-Agent picker. Author new ones via Author Skill.
 */
export default function SkillsPage() {
  const [items, setItems] = useState<RegistryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api
      .listRegistrySkills()
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
            <Sparkles className="w-5 h-5 text-indigo-400" /> Skills
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Reusable capabilities catalogued in the registry. Pick them when composing an agent.
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
            href="/skills/new"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium whitespace-nowrap"
          >
            <Plus className="w-4 h-4" /> Author Skill
          </Link>
        </div>
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="panel p-6 text-sm text-[var(--ink-300)]">
          No skills yet. <Link href="/skills/new" className="text-indigo-400 hover:underline">Author one</Link>.
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
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
