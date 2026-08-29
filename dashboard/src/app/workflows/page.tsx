"use client";

/**
 * Workflows — the lifecycle HOME (DASH-5).
 *
 * One page to define → run → observe: every runnable blueprint is a card with a
 * mini DAG preview, its description, a Run button (opens the shared trigger
 * flow with that blueprint pre-selected), and its most recent runs linking to
 * /runs/[id]. A GuidancePanel up top explains how it all fits together.
 *
 * The GitHub-issue Kanban that used to live here moved to /board.
 *
 * Data:
 *   api.listBlueprints()              → the blueprint cards
 *   api.getBlueprintGraph(name)       → each card's mini DAG (lazy, per card)
 *   api.listRuns({ blueprint })       → each card's recent runs
 *   api.triggerPipeline(repo, …, {blueprint}) → dispatch (via TriggerDialog)
 *
 * ?action=new auto-opens the trigger flow (the sidebar / ⌘K "New task" CTA
 * routes here with that query — DASH-9).
 */

import Link from "next/link";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { GitBranch, Layers, Play, Plus, RefreshCw } from "lucide-react";

import {
  api,
  type BlueprintGraph,
  type BlueprintSummary,
  type PipelineRun,
} from "@/lib/api";
import { BlueprintDAG } from "@/components/blueprint-dag";
import { BlueprintMesh } from "@/components/blueprint-mesh";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { EmptyState } from "@/components/empty-state";
import { GuidanceInfo, GuidancePanel, HelpPopover } from "@/components/guidance";
import { RunStateBadge } from "@/components/run-state-badge";
import { TriggerDialog } from "@/components/trigger-dialog";
import { runDetailHref } from "@/lib/run-entry";

export default function WorkflowsPage() {
  return (
    <Suspense fallback={<WorkflowsSkeleton />}>
      <WorkflowsHome />
    </Suspense>
  );
}

function WorkflowsHome() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [blueprints, setBlueprints] = useState<BlueprintSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // The trigger flow, pre-bound to a chosen blueprint.
  const [triggerBlueprint, setTriggerBlueprint] = useState<string | null>(null);
  const [triggerRepo, setTriggerRepo] = useState("");

  const load = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const list = await api.listBlueprints();
      setBlueprints(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBlueprints(null);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // ?action=new opens the trigger flow with no blueprint pre-bound — the
  // backend picks the repo-appropriate default. (To run a specific blueprint,
  // use its card's Run button.) We open with "" (not null) to mean "open,
  // unbound". Clear the query so a refresh doesn't re-open it.
  useEffect(() => {
    if (searchParams.get("action") === "new") {
      setTriggerRepo(searchParams.get("repo") ?? "");
      setTriggerBlueprint("");
      router.replace("/workflows");
    }
  }, [searchParams, router]);

  const handleTrigger = useCallback(
    async (
      repo: string,
      requirements: string,
      opts?: { blueprint?: string; autonomy?: string; brainstorm?: boolean },
    ) => {
      const result = await api.triggerPipeline(repo, requirements, {
        ...opts,
        blueprint: triggerBlueprint || opts?.blueprint,
      });
      setTriggerBlueprint(null);
      setTriggerRepo("");
      if (result?.run_id) router.push(runDetailHref(result.run_id));
    },
    [triggerBlueprint, router],
  );

  return (
    <div className="p-7 space-y-5 min-h-full">
      <Breadcrumbs items={[{ label: "Fleet", href: "/" }, { label: "Workflows" }]} />

      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Lifecycle</div>
          <h1
            className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
            style={{ color: "var(--ink-strong)" }}
          >
            <Layers className="w-5 h-5" style={{ color: "var(--accent)" }} />
            Workflows
            <GuidanceInfo id="workflows" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm mt-1 max-w-2xl" style={{ color: "var(--ink-soft)" }}>
            Pick a blueprint, point it at a repo, and dispatch a run. Each blueprint is a fixed DAG of
            stages — runs differ by which stages skip, never by adding or reordering them.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => {
              setTriggerRepo("");
              setTriggerBlueprint("");
            }}
            className="btn-primary !py-1.5"
          >
            <Plus className="w-3.5 h-3.5" /> New run
          </button>
          <Link href="/workflows/new" className="btn-secondary">
            <GitBranch className="w-3.5 h-3.5" /> Build blueprint
          </Link>
          <button onClick={load} disabled={refreshing} className="btn-secondary">
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} /> Refresh
          </button>
        </div>
      </header>

      <GuidancePanel id="workflows" />

      {error && (
        <div
          className="rounded-md px-3 py-2 text-sm font-mono flex items-start gap-2"
          style={{
            background: "var(--error-soft-bg)",
            color: "var(--error-ink)",
            border: "1px solid var(--error-soft-bd)",
          }}
        >
          <div>
            <div className="font-semibold">Pipeline runtime unavailable</div>
            <div className="text-xs mt-0.5 opacity-90">{error}</div>
          </div>
        </div>
      )}

      {loading ? (
        <WorkflowsCardsSkeleton />
      ) : !blueprints || blueprints.length === 0 ? (
        !error && (
          <div className="panel">
            <EmptyState
              Icon={Layers}
              title="No blueprints registered"
              description="The runtime hasn't loaded any blueprint YAMLs yet. Build your own, or browse the catalog."
              action={{ label: "Build a blueprint", href: "/workflows/new" }}
              secondary={{ label: "Browse blueprints", href: "/blueprint" }}
            />
          </div>
        )
      ) : (
        <section className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {blueprints.map((b) => (
            <BlueprintCard
              key={b.name}
              blueprint={b}
              onRun={() => {
                setTriggerRepo("");
                setTriggerBlueprint(b.name);
              }}
            />
          ))}
        </section>
      )}

      {/* Shared trigger flow, pre-bound to the chosen blueprint. The dialog is
          generic (repo + requirements); we thread the blueprint via the parent
          handler so the backend dispatches the right DAG (DASH-6). */}
      <TriggerDialog
        open={triggerBlueprint !== null}
        initialRepo={triggerRepo}
        onClose={() => {
          setTriggerBlueprint(null);
          setTriggerRepo("");
        }}
        onTrigger={handleTrigger}
      />
    </div>
  );
}

// ── Blueprint card ───────────────────────────────────────────────────────

function BlueprintCard({
  blueprint,
  onRun,
}: {
  blueprint: BlueprintSummary;
  onRun: () => void;
}) {
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [graphErr, setGraphErr] = useState(false);
  const [runs, setRuns] = useState<PipelineRun[] | null>(null);
  // Per-card flow view: the lane DAG (default) or the force-directed mesh — the
  // same two renderers as the run flow view (pipeline-flow.tsx).
  const [view, setView] = useState<"dag" | "mesh">("dag");

  // Lazily fetch the DAG + recent runs for this blueprint.
  useEffect(() => {
    let cancelled = false;
    api
      .getBlueprintGraph(blueprint.name)
      .then((g) => !cancelled && setGraph(g))
      .catch(() => !cancelled && setGraphErr(true));
    api
      .listRuns({ blueprint: blueprint.name, limit: 3 })
      .then((r) => !cancelled && setRuns(r))
      .catch(() => !cancelled && setRuns([]));
    return () => {
      cancelled = true;
    };
  }, [blueprint.name]);

  const isSupervisor = blueprint.name === "supervisor-alm";

  return (
    <article className="panel p-4 flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2
              className="font-mono text-[15px] font-medium truncate"
              style={{ color: "var(--ink-strong)" }}
            >
              {blueprint.title || blueprint.name}
            </h2>
            <span
              className="inline-flex items-center gap-1 text-[11px]"
              style={{ color: "var(--ink-muted)" }}
            >
              <GitBranch className="w-3 h-3" />
              {blueprint.stageCount} stage{blueprint.stageCount === 1 ? "" : "s"}
            </span>
            {isSupervisor && (
              <span className="inline-flex items-center gap-1">
                <span className="pill pill-muted !text-[9px]">advisory plan</span>
                <HelpPopover term="dynamic" />
              </span>
            )}
          </div>
          {blueprint.description && (
            <p
              className="mt-1 text-[13px] leading-snug line-clamp-2"
              style={{ color: "var(--ink-soft)" }}
            >
              {blueprint.description}
            </p>
          )}
        </div>
        <button type="button" onClick={onRun} className="btn-primary !py-1.5 shrink-0">
          <Play className="w-3.5 h-3.5" /> Run
        </button>
      </div>

      {/* Mini flow — static (no overlay) "what this blueprint does" preview.
          Toggle between the lane DAG and the force-directed mesh. */}
      <div className="panel-muted p-2 overflow-hidden">
        {graph && graph.nodes.length > 0 && (
          <div className="mb-1.5 flex items-center justify-end gap-1 text-[10px]">
            <button
              type="button"
              aria-pressed={view === "dag"}
              onClick={() => setView("dag")}
              className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition ${
                view === "dag" ? "bg-[var(--surface-muted)] text-[var(--ink-strong)]" : "text-[var(--ink-muted)] hover:text-[var(--ink-soft)]"
              }`}
            >
              <GitBranch className="w-3 h-3" /> DAG
            </button>
            <button
              type="button"
              aria-pressed={view === "mesh"}
              onClick={() => setView("mesh")}
              className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition ${
                view === "mesh" ? "bg-[var(--surface-muted)] text-[var(--ink-strong)]" : "text-[var(--ink-muted)] hover:text-[var(--ink-soft)]"
              }`}
            >
              <Layers className="w-3 h-3" /> Mesh
            </button>
          </div>
        )}
        {graph ? (
          view === "mesh" ? (
            <BlueprintMesh graph={graph} />
          ) : (
            <BlueprintDAG graph={graph} dense />
          )
        ) : graphErr ? (
          <p className="text-[12px] px-1 py-3" style={{ color: "var(--ink-muted)" }}>
            Flow unavailable for <span className="font-mono">{blueprint.name}</span>.
          </p>
        ) : (
          <div
            className="animate-pulse rounded"
            style={{ height: 72, background: "var(--surface-muted)" }}
          />
        )}
      </div>

      {/* Recent runs of this blueprint. */}
      <RecentRuns runs={runs} blueprintName={blueprint.name} />
    </article>
  );
}

function RecentRuns({
  runs,
  blueprintName,
}: {
  runs: PipelineRun[] | null;
  blueprintName: string;
}) {
  return (
    <div className="border-t pt-2.5" style={{ borderColor: "var(--border-subtle)" }}>
      <div className="flex items-center justify-between mb-1.5">
        <span className="label-eyebrow">Recent runs</span>
        <Link
          href={`/runs?blueprint=${encodeURIComponent(blueprintName)}`}
          className="text-[11px] hover:underline underline-offset-2"
          style={{ color: "var(--accent)" }}
        >
          View all
        </Link>
      </div>
      {runs === null ? (
        <div className="space-y-1.5">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="animate-pulse rounded"
              style={{ height: 28, background: "var(--surface-muted)" }}
            />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <p className="text-[12px] py-1" style={{ color: "var(--ink-muted)" }}>
          No runs yet — Run it to see the DAG fill in live.
        </p>
      ) : (
        <ul className="space-y-1">
          {runs.map((r) => (
            <li key={r.run_id}>
              <Link
                href={`/runs/${r.run_id}`}
                className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 transition-colors hover:bg-[var(--surface-hover)]"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span className="font-mono text-[11px]" style={{ color: "var(--ink-muted)" }}>
                    {(r.run_id ?? "").slice(0, 8)}
                  </span>
                  <span
                    className="text-[12px] truncate"
                    style={{ color: "var(--ink-strong)" }}
                  >
                    {r.repo || "—"}
                  </span>
                </span>
                <RunStateBadge state={r.stage} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Loading skeletons ──────────────────────────────────────────────────────

function WorkflowsCardsSkeleton() {
  return (
    <section className="grid grid-cols-1 xl:grid-cols-2 gap-4" aria-busy>
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="panel p-4 space-y-3">
          <div className="animate-pulse rounded" style={{ height: 22, width: "40%", background: "var(--surface-muted)" }} />
          <div className="animate-pulse rounded" style={{ height: 80, background: "var(--surface-muted)" }} />
          <div className="animate-pulse rounded" style={{ height: 40, background: "var(--surface-muted)" }} />
        </div>
      ))}
    </section>
  );
}

function WorkflowsSkeleton() {
  return (
    <div className="p-7 space-y-5 min-h-full">
      <div className="animate-pulse rounded" style={{ height: 18, width: 160, background: "var(--surface-muted)" }} />
      <div className="animate-pulse rounded" style={{ height: 32, width: 220, background: "var(--surface-muted)" }} />
      <WorkflowsCardsSkeleton />
    </div>
  );
}
