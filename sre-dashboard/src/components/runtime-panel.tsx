"use client";

// Runtime panel for the SRE dashboard: the published blueprints the SRE
// runtime can run, a one-click "run now", per-blueprint scheduling, a
// level-based DAG view, and schedule management.

import { useCallback, useEffect, useState } from "react";
import {
  sre,
  type BlueprintGraph,
  type ObservabilitySource,
  type SREBlueprint,
  type SRESchedule,
} from "@/lib/api";
import { useToast } from "@/components/toast";
import { Explainer } from "@/components/explainer";

// Normalize an unknown thrown value to a readable string.
function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

export function RuntimePanel({ mode }: { mode: "blueprints" | "schedules" | "sources" }) {
  if (mode === "blueprints") return <BlueprintsPanel />;
  if (mode === "schedules") return <SchedulesPanel />;
  return <SourcesPanel />;
}

function SourcesPanel() {
  const [sources, setSources] = useState<ObservabilitySource[]>([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await sre.observabilitySources();
      setSources(r.sources);
      if (r.error) setError(r.error);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-3">
      <Explainer id="sources" />
      {error && <div className="text-sm text-amber-600 dark:text-amber-400">{error}</div>}
      {loaded && sources.length === 0 && (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">
          No observability sources connected. The in-cluster Prometheus is used by default; add Datadog, New Relic,
          CloudWatch, Azure Monitor, Elasticsearch, or Grafana in DevAI Settings.
        </div>
      )}
      {sources.map((s) => (
        <div
          key={s.provider}
          className="flex items-center justify-between rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-3"
        >
          <div className="flex items-center gap-3">
            <span
              className={`h-2.5 w-2.5 rounded-full ${s.ok ? "bg-emerald-500" : "bg-red-500"}`}
              title={s.ok ? "healthy" : "unreachable"}
            />
            <span className="font-medium text-zinc-900 dark:text-zinc-100 capitalize">
              {s.provider.replace("_", " ")}
            </span>
          </div>
          <span className="text-xs text-zinc-500 dark:text-zinc-400 truncate max-w-[60%]">{s.detail}</span>
        </div>
      ))}
    </div>
  );
}

function BlueprintsPanel() {
  const [items, setItems] = useState<SREBlueprint[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [graphFor, setGraphFor] = useState<string>("");
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [scheduleFor, setScheduleFor] = useState<string>("");
  const [cadence, setCadence] = useState("hourly");
  const [toast, setToast] = useState("");

  const load = useCallback(async () => {
    try {
      setItems(await sre.listBlueprints());
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  async function run(name: string) {
    setBusy(name);
    setToast("");
    try {
      const r = await sre.triggerBlueprint(name);
      setToast(`Triggered ${name} · scan ${r.scan_id.slice(0, 8)}`);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  async function showGraph(name: string) {
    if (graphFor === name) {
      setGraphFor("");
      setGraph(null);
      return;
    }
    setGraphFor(name);
    setGraph(null);
    try {
      setGraph(await sre.blueprintGraph(name));
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }

  async function schedule(name: string) {
    setBusy(name);
    try {
      await sre.createSchedule({ blueprint: name, cron: cadence });
      setScheduleFor("");
      setToast(`Scheduled ${name} · ${cadence}`);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-3">
      <Explainer id="blueprints" />
      {error && <div className="text-sm text-red-600 dark:text-red-400">{error}</div>}
      {toast && <div className="text-sm text-emerald-600 dark:text-emerald-400">{toast}</div>}
      {items.length === 0 && (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">
          No SRE blueprints available yet. Publish one from DevAI SRE Studio.
        </div>
      )}
      {items.map((b) => (
        <div
          key={b.name}
          className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-medium text-zinc-900 dark:text-zinc-100">{b.title || b.name}</span>
                {b.pattern && (
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                    {b.pattern}
                  </span>
                )}
                {b.kind && (
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                    {b.kind}
                  </span>
                )}
              </div>
              <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5 line-clamp-2">{b.description}</p>
              <p className="text-xs text-zinc-400 dark:text-zinc-500 mt-1">
                {b.stage_count} stages{b.cadence ? ` · default cadence ${b.cadence}` : ""}
              </p>
            </div>
            <div className="flex flex-col gap-1.5 shrink-0">
              <button
                onClick={() => run(b.name)}
                disabled={busy === b.name}
                className="text-xs px-3 py-1.5 rounded bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 disabled:opacity-50"
              >
                {busy === b.name ? "…" : "Run now"}
              </button>
              <button
                onClick={() => setScheduleFor(scheduleFor === b.name ? "" : b.name)}
                className="text-xs px-3 py-1.5 rounded border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300"
              >
                Schedule
              </button>
              <button
                onClick={() => showGraph(b.name)}
                className="text-xs px-3 py-1.5 rounded border border-zinc-300 dark:border-zinc-700 text-zinc-700 dark:text-zinc-300"
              >
                {graphFor === b.name ? "Hide flow" : "Flow"}
              </button>
            </div>
          </div>

          {scheduleFor === b.name && (
            <div className="mt-3 flex items-center gap-2">
              <input
                value={cadence}
                onChange={(e) => setCadence(e.target.value)}
                placeholder="hourly · daily · 15m · 6h"
                className="text-xs px-2 py-1.5 rounded border border-zinc-300 dark:border-zinc-700 bg-transparent text-zinc-900 dark:text-zinc-100"
              />
              <button
                onClick={() => schedule(b.name)}
                className="text-xs px-3 py-1.5 rounded bg-emerald-600 text-white"
              >
                Save schedule
              </button>
            </div>
          )}

          {graphFor === b.name && graph && <FlowGraph graph={graph} />}
        </div>
      ))}
    </div>
  );
}

function FlowGraph({ graph }: { graph: BlueprintGraph }) {
  // Level-based rendering: each topo level is a row; parallel stages sit
  // side by side. Keeps the SVG-free DAG readable in the SRE dashboard.
  return (
    <div className="mt-3 overflow-x-auto rounded border border-zinc-200 dark:border-zinc-800 p-3 bg-zinc-50 dark:bg-zinc-950">
      <div className="space-y-2">
        {graph.levels.map((level, i) => (
          <div key={i} className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] w-10 shrink-0 text-zinc-400">L{i + 1}</span>
            {level.map((stageName) => {
              const node = graph.nodes.find((n) => n.name === stageName);
              return (
                <span
                  key={stageName}
                  title={node?.stage}
                  className="text-xs px-2 py-1 rounded border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200"
                >
                  {node?.title || stageName}
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function SchedulesPanel() {
  const toast = useToast();
  const [items, setItems] = useState<SRESchedule[]>([]);
  const [error, setError] = useState("");
  // Per-schedule busy flag so a row's buttons disable while its mutation runs.
  const [busyId, setBusyId] = useState("");

  const load = useCallback(async () => {
    try {
      setItems(await sre.listSchedules());
      setError("");
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  // Toggle/delete were fire-and-forget: a rejected promise was unhandled, so a
  // failed enable/disable or delete silently looked like it worked. Wrap each
  // in try/catch with a busy state + error toast (DASH-11).
  async function toggle(s: SRESchedule) {
    if (busyId) return;
    setBusyId(s.id);
    try {
      await sre.updateSchedule(s.id, { enabled: !s.enabled });
      toast.success(`${s.blueprint} schedule ${s.enabled ? "disabled" : "enabled"}`);
      await load();
    } catch (e) {
      toast.error(`Could not update schedule: ${errMsg(e)}`);
    } finally {
      setBusyId("");
    }
  }

  async function remove(s: SRESchedule) {
    if (busyId) return;
    setBusyId(s.id);
    try {
      await sre.deleteSchedule(s.id);
      toast.success(`Deleted ${s.blueprint} schedule`);
      await load();
    } catch (e) {
      toast.error(`Could not delete schedule: ${errMsg(e)}`);
    } finally {
      setBusyId("");
    }
  }

  return (
    <div className="space-y-2">
      <Explainer id="schedules" />
      {error && <div className="text-sm text-red-600 dark:text-red-400">{error}</div>}
      {items.length === 0 && (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">
          No schedules. Add one from the Blueprints tab.
        </div>
      )}
      {items.map((s) => (
        <div
          key={s.id}
          className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-3"
        >
          <div className="min-w-0">
            <span className="font-medium text-zinc-900 dark:text-zinc-100">{s.blueprint}</span>
            <span className="text-sm text-zinc-500 dark:text-zinc-400 ml-2">{s.cron}</span>
            <div className="text-xs text-zinc-400 dark:text-zinc-500">
              cluster {s.cluster_id}
              {s.last_run_at ? ` · last run ${new Date(s.last_run_at).toLocaleString()}` : " · never run"}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => toggle(s)}
              disabled={busyId === s.id}
              className={`text-xs px-3 py-1.5 rounded disabled:opacity-50 disabled:cursor-wait ${
                s.enabled
                  ? "bg-emerald-600 text-white"
                  : "border border-zinc-300 dark:border-zinc-700 text-zinc-600 dark:text-zinc-400"
              }`}
            >
              {s.enabled ? "Enabled" : "Disabled"}
            </button>
            <button
              onClick={() => remove(s)}
              disabled={busyId === s.id}
              className="text-xs px-3 py-1.5 rounded text-red-600 dark:text-red-400 disabled:opacity-50 disabled:cursor-wait"
            >
              {busyId === s.id ? "…" : "Delete"}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
