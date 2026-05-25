"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  GitMerge,
  Layers,
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react";

type Blueprint = {
  name: string;
  description?: string;
  stages?: Array<{
    name: string;
    stage?: string;
    type?: string;
    depends_on?: string[];
    parallel?: boolean;
    condition?: string;
  }>;
  metadata?: Record<string, unknown>;
};

type BlueprintLane = {
  name: string;
  stage?: string;
  depends_on?: string[];
  parallel?: boolean;
};

export default function BlueprintPage() {
  const [bps, setBps] = useState<Blueprint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [active, setActive] = useState<string | null>(null);

  async function load() {
    setRefreshing(true);
    setError(null);
    try {
      const r = await fetch("/api/pipeline/blueprints");
      if (!r.ok) {
        const body = (await r.text()).slice(0, 240);
        throw new Error(`HTTP ${r.status}: ${body}`);
      }
      const data = await r.json();
      const list = Array.isArray(data) ? data : data.blueprints || [];
      setBps(list);
      if (list.length > 0 && active === null) setActive(list[0].name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeBp = bps.find((b) => b.name === active) ?? bps[0];

  return (
    <div className="p-7 space-y-5 min-h-full">
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Pipeline</div>
          <h1
            className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
            style={{ color: "var(--ink-strong)" }}
          >
            <Layers className="w-5 h-5" style={{ color: "var(--accent)" }} />
            Blueprints
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
            DAGs of stages — each blueprint composes deterministic and agentic
            stages into a runnable pipeline. The default <code>app-scaffold</code>{" "}
            powers the &ldquo;build me an app&rdquo; live-preview flow.
          </p>
        </div>
        <button
          onClick={load}
          disabled={refreshing}
          className="btn-secondary"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </header>

      {error && (
        <div
          className="rounded-md px-3 py-2 text-sm font-mono flex items-start gap-2"
          style={{
            background: "var(--error-soft-bg)",
            color: "var(--error-ink)",
            border: "1px solid var(--error-soft-bd)",
          }}
        >
          <AlertTriangle className="w-4 h-4 mt-0.5" />
          <div>
            <div className="font-semibold">Pipeline runtime unavailable</div>
            <div className="text-xs mt-0.5 opacity-90">{error}</div>
            <div className="text-xs mt-1 opacity-80">
              Set <code>DEVAI_PIPELINE_ENABLED=true</code> on the devai-api
              deployment and roll the pod.
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="panel p-8 text-sm flex items-center gap-2" style={{ color: "var(--ink-soft)" }}>
          <Loader2 className="w-4 h-4 animate-spin" />
          Loading registered blueprints…
        </div>
      ) : bps.length === 0 && !error ? (
        <div className="panel p-8 text-center" style={{ color: "var(--ink-soft)" }}>
          <p className="text-sm">
            No blueprints registered. The runtime probably hasn&apos;t loaded any
            YAMLs from <code>blueprints/</code> yet.
          </p>
        </div>
      ) : (
        <section className="grid grid-cols-12 gap-4">
          {/* Sidebar — list of blueprints */}
          <aside className="col-span-12 lg:col-span-3">
            <div className="label-eyebrow mb-2">Available</div>
            <ul className="space-y-1">
              {bps.map((b) => {
                const isActive = activeBp?.name === b.name;
                const isDefault =
                  b.metadata?.default_for_blank_repo === true ||
                  b.name === "app-scaffold";
                return (
                  <li key={b.name}>
                    <button
                      type="button"
                      onClick={() => setActive(b.name)}
                      className="w-full text-left px-3 py-2 rounded-md text-sm transition-colors"
                      style={{
                        background: isActive ? "var(--accent-soft-bg-2)" : "transparent",
                        color: isActive ? "var(--accent-soft-ink)" : "var(--ink)",
                        border: `1px solid ${
                          isActive ? "var(--accent-soft-bd)" : "transparent"
                        }`,
                      }}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{b.name}</span>
                        {isDefault && (
                          <Sparkles
                            className="w-3 h-3"
                            style={{ color: "var(--accent)" }}
                          />
                        )}
                      </div>
                      <div className="text-xs mt-0.5" style={{ color: "var(--ink-muted)" }}>
                        {(b.stages?.length ?? 0)} stages
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </aside>

          {/* Detail */}
          <main className="col-span-12 lg:col-span-9">
            {activeBp && (
              <article className="panel p-5">
                <div className="flex items-baseline justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <Boxes className="w-4 h-4" style={{ color: "var(--accent)" }} />
                    <h2 className="font-serif text-lg font-medium" style={{ color: "var(--ink-strong)" }}>
                      {activeBp.name}
                    </h2>
                  </div>
                  <span className="label-eyebrow">
                    {activeBp.stages?.length ?? 0} stages
                  </span>
                </div>
                {activeBp.description && (
                  <p className="text-sm mt-2 whitespace-pre-line" style={{ color: "var(--ink-soft)" }}>
                    {activeBp.description}
                  </p>
                )}

                <div className="mt-5">
                  <div className="label-eyebrow mb-2">Stage DAG</div>
                  <StageDag stages={(activeBp.stages ?? []) as BlueprintLane[]} />
                </div>
              </article>
            )}
          </main>
        </section>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function StageDag({ stages }: { stages: BlueprintLane[] }) {
  // Topo-sort into levels so users can see what runs in parallel.
  const levels = topoLevels(stages);
  return (
    <div className="space-y-3">
      {levels.map((lvl, idx) => (
        <div key={idx} className="flex items-center gap-2 flex-wrap">
          {lvl.map((s, j) => (
            <div key={s.name} className="flex items-center gap-2">
              <span
                className="text-xs px-2.5 py-1 rounded font-mono"
                style={{
                  background: "var(--surface-muted)",
                  border: "1px solid var(--border-subtle)",
                  color: "var(--ink-strong)",
                }}
                title={
                  s.depends_on?.length
                    ? `depends_on: ${s.depends_on.join(", ")}`
                    : "no dependencies"
                }
              >
                {s.parallel && (
                  <GitMerge
                    className="inline w-3 h-3 mr-1 align-middle"
                    style={{ color: "var(--accent)" }}
                  />
                )}
                {s.name}
                {s.stage && s.stage !== s.name && (
                  <span
                    className="ml-1.5"
                    style={{ color: "var(--ink-muted)" }}
                  >
                    · {s.stage}
                  </span>
                )}
              </span>
              {j < lvl.length - 1 && <span style={{ color: "var(--ink-muted)" }}>·</span>}
            </div>
          ))}
          {idx < levels.length - 1 && (
            <ArrowRight
              className="w-3.5 h-3.5 ml-1"
              style={{ color: "var(--ink-muted)" }}
            />
          )}
        </div>
      ))}
    </div>
  );
}

/* Kahn-style level grouping so the dashboard can show parallel lanes. */
function topoLevels(stages: BlueprintLane[]): BlueprintLane[][] {
  const remaining = new Map(stages.map((s) => [s.name, s] as const));
  const levels: BlueprintLane[][] = [];
  while (remaining.size > 0) {
    const ready: BlueprintLane[] = [];
    for (const s of remaining.values()) {
      const deps = s.depends_on ?? [];
      if (deps.every((d) => !remaining.has(d))) ready.push(s);
    }
    if (ready.length === 0) {
      // Cycle / dangling dep — flush the rest in one lane so the UI still renders.
      levels.push([...remaining.values()]);
      break;
    }
    levels.push(ready);
    for (const r of ready) remaining.delete(r.name);
  }
  return levels;
}
