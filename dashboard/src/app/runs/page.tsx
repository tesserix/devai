"use client";

/**
 * /runs — every blueprint execution, filterable (DASH-8).
 *
 * The dedicated runs index: lists api.listRuns() with blueprint / repo / state /
 * source filters and offset pagination, each row a link into /runs/<id>. The
 * Fleet home keeps the dense recent-runs sidebar; this page is the full,
 * filterable ledger.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, type BlueprintSummary, type PipelineRun, type RunFilters } from "@/lib/api";
import { RunList } from "@/components/run-list";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { EmptyState } from "@/components/empty-state";
import { type RunState } from "@/components/run-state-badge";

const PAGE_SIZE = 25;

// The states worth filtering by — the canonical set, minus the noisy ones.
const STATE_OPTIONS: { value: RunState | ""; label: string }[] = [
  { value: "", label: "Any state" },
  { value: "running", label: "Running" },
  { value: "gate-pending", label: "Awaiting approval" },
  { value: "done", label: "Done" },
  { value: "failed", label: "Failed" },
  { value: "paused", label: "Paused" },
  { value: "cancelled", label: "Cancelled" },
];

const SOURCE_OPTIONS = ["", "webhook", "manual", "compose", "scaffold", "api"];

export default function RunsIndexPage() {
  const [runs, setRuns] = useState<PipelineRun[]>([]);
  const [blueprints, setBlueprints] = useState<BlueprintSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [repo, setRepo] = useState("");
  const [blueprint, setBlueprint] = useState("");
  const [state, setState] = useState<RunState | "">("");
  const [source, setSource] = useState("");
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    api.listBlueprints().then(setBlueprints).catch(() => setBlueprints([]));
  }, []);

  const filters = useMemo<RunFilters>(
    () => ({
      repo: repo.trim() || undefined,
      blueprint: blueprint || undefined,
      state: state || undefined,
      source: source || undefined,
      limit: PAGE_SIZE,
      offset,
    }),
    [repo, blueprint, state, source, offset],
  );

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listRuns(filters);
      setRuns(data);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
      setRuns([]);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  // Debounce the repo text input; other filters fire immediately.
  useEffect(() => {
    const t = setTimeout(fetchRuns, 250);
    return () => clearTimeout(t);
  }, [fetchRuns]);

  // Live-refresh the current page on an interval (matches the rest of the app).
  useEffect(() => {
    const id = setInterval(fetchRuns, 8000);
    return () => clearInterval(id);
  }, [fetchRuns]);

  function resetOffset<T>(setter: (v: T) => void) {
    return (v: T) => {
      setOffset(0);
      setter(v);
    };
  }

  const hasFilters = !!(repo || blueprint || state || source);
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const canPrev = offset > 0;
  const canNext = runs.length === PAGE_SIZE;

  return (
    <div className="p-7 space-y-5 min-h-full">
      <Breadcrumbs items={[{ label: "Fleet", href: "/" }, { label: "Runs" }]} />

      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Lifecycle</div>
          <h1 className="font-serif text-2xl font-medium mt-1" style={{ color: "var(--ink-strong)" }}>
            Runs
            <GuidanceInfo id="runs" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm mt-1 max-w-2xl" style={{ color: "var(--ink-soft)" }}>
            Every execution of a blueprint against a repo. Filter by blueprint, repo, state or source, and open a run to watch its DAG fill in live.
          </p>
        </div>
        <button onClick={fetchRuns} disabled={loading} className="btn-secondary shrink-0">
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh
        </button>
      </header>

      <GuidancePanel id="runs" />

      {/* Filter bar */}
      <div
        className="rounded-lg border p-3 flex flex-wrap items-end gap-3"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        <Field label="Repository">
          <input
            value={repo}
            onChange={(e) => resetOffset(setRepo)(e.target.value)}
            placeholder="owner/name"
            className="field w-56"
          />
        </Field>
        <Field label="Blueprint">
          <select
            value={blueprint}
            onChange={(e) => resetOffset(setBlueprint)(e.target.value)}
            className="field w-48"
          >
            <option value="">Any blueprint</option>
            {blueprints.map((b) => (
              <option key={b.name} value={b.name}>
                {b.title || b.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="State">
          <select
            value={state}
            onChange={(e) => resetOffset<RunState | "">(setState)(e.target.value as RunState | "")}
            className="field w-44"
          >
            {STATE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Source">
          <select
            value={source}
            onChange={(e) => resetOffset(setSource)(e.target.value)}
            className="field w-40"
          >
            {SOURCE_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s === "" ? "Any source" : s}
              </option>
            ))}
          </select>
        </Field>
        {hasFilters && (
          <button
            type="button"
            onClick={() => {
              setOffset(0);
              setRepo("");
              setBlueprint("");
              setState("");
              setSource("");
            }}
            className="btn-ghost !text-sm"
          >
            Clear filters
          </button>
        )}
      </div>

      {error && (
        <div
          className="rounded-md px-3 py-2 text-sm font-mono"
          style={{ background: "var(--error-soft-bg)", color: "var(--error-ink)", border: "1px solid var(--error-soft-bd)" }}
        >
          {error}
        </div>
      )}

      {/* Results */}
      <div
        className="rounded-lg border overflow-hidden"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        {loading && runs.length === 0 ? (
          <ul className="divide-y" style={{ borderColor: "var(--border-subtle)" }}>
            {[0, 1, 2, 3].map((i) => (
              <li key={i} className="px-4 py-3.5">
                <div className="h-4 rounded animate-pulse" style={{ background: "var(--surface-muted)", width: "40%" }} />
              </li>
            ))}
          </ul>
        ) : runs.length === 0 ? (
          <EmptyState
            title={hasFilters ? "No runs match these filters" : "No runs yet"}
            description={
              hasFilters
                ? "Try widening the filters, or clear them to see every run."
                : "Dispatch a blueprint against a repo from Workflows to create your first run."
            }
            action={hasFilters ? undefined : { label: "Run a workflow", href: "/workflows" }}
            secondary={hasFilters ? undefined : { label: "Browse blueprints", href: "/blueprint" }}
          />
        ) : (
          <RunList runs={runs} variant="rows" />
        )}
      </div>

      {/* Pagination */}
      {(canPrev || canNext) && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 text-[12px] tabular-nums" style={{ color: "var(--ink-muted)" }}>
            <span>Page {page}</span>
            <span aria-hidden>·</span>
            <span>
              Showing {offset + 1}–{offset + runs.length}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
              disabled={!canPrev}
              className="btn-secondary !text-sm"
              style={!canPrev ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() => setOffset((o) => o + PAGE_SIZE)}
              disabled={!canNext}
              className="btn-secondary !text-sm"
              style={!canNext ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="label-eyebrow">{label}</span>
      {children}
    </label>
  );
}
