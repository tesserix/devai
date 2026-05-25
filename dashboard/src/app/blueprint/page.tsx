"use client";

import { useEffect, useState } from "react";
import { Layers } from "lucide-react";

type Blueprint = {
  name: string;
  description?: string;
  stages?: Array<{ name: string; type: string; stage?: string; depends_on?: string[] }>;
};

export default function BlueprintPage() {
  const [bps, setBps] = useState<Blueprint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/pipeline/blueprints")
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
        return r.json();
      })
      .then((data) => setBps(Array.isArray(data) ? data : data.blueprints || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="p-7 space-y-6">
      <header>
        <div className="label-eyebrow">Pipeline</div>
        <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
          <Layers className="w-5 h-5 text-indigo-400" /> Blueprints
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          DAGs of stages. Each blueprint composes deterministic + agentic stages into a runnable pipeline.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : bps.length === 0 ? (
        <div className="text-sm text-[var(--ink-500)]">
          No blueprints registered. The pipeline runtime must be enabled (DEVAI_PIPELINE_ENABLED=true) for this list to populate.
        </div>
      ) : (
        <div className="space-y-3">
          {bps.map((b) => (
            <article
              key={b.name}
              className="panel p-4"
            >
              <div className="flex items-baseline justify-between gap-3">
                <h2 className="text-base font-display font-medium text-[var(--ink-50)]">{b.name}</h2>
                <span className="label-eyebrow">
                  {b.stages?.length ?? 0} stages
                </span>
              </div>
              {b.description && (
                <p className="text-sm text-[var(--ink-300)] mt-1">{b.description}</p>
              )}
              {b.stages && b.stages.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {b.stages.map((s) => (
                    <span
                      key={s.name}
                      title={
                        s.depends_on?.length ? `depends on: ${s.depends_on.join(", ")}` : undefined
                      }
                      className="text-xs px-2 py-0.5 rounded font-mono border border-[var(--surface-border)] bg-[var(--surface-2)] text-[var(--ink-100)]"
                    >
                      {s.name}
                    </span>
                  ))}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
