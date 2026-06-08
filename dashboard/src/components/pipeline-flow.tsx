"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type BlueprintGraph,
  type PipelineRun,
} from "@/lib/api";
import { BlueprintDAG, type RunStateOverlay } from "@/components/blueprint-dag";
import { normalizeRunState, type RunState } from "@/components/run-state-badge";

/**
 * pipeline-flow — the run-aware DAG surfaces, both built on the one canonical
 * <BlueprintDAG> renderer (DASH-7). Nothing here re-implements layout; this
 * module is the data layer that fetches a blueprint graph and projects a run's
 * live state onto it.
 *
 * Exports:
 *   - buildRunOverlay(run, graph?) → the name→{state,durationSeconds} overlay
 *     used by BlueprintDAG. Pure; derives per-stage state from
 *     stages_completed / stages_skipped / stages_failed + the active stage, and
 *     per-stage durations from stage_events. Gate stages awaiting a decision
 *     become "gate-pending".
 *   - <RunDAG> — the run-detail consumer: pass the run + graph + gate handlers
 *     and it draws the coloured DAG with inline approve/reject affordances.
 *   - <PipelineFlow> — the Fleet-home consumer kept on its original
 *     currentStage-based API so the dashboard index needs no change.
 */

// A pending gate the user can act on — gate name → true. Lets the overlay flip
// the matching stage to "gate-pending" so the inline Approve/Reject show up.
export type PendingGates = Record<string, boolean>;

interface RunStageShape {
  stages_completed?: string[];
  stages_skipped?: string[];
  stages_failed?: string[];
  stage_events?: Array<{
    stage: string;
    phase?: string;
    duration_ms?: number;
  }>;
  current_stage?: string;
  stage?: string;
  state?: string;
}

/**
 * Project a run's recorded progress onto a blueprint graph as an overlay.
 *
 * Precedence per stage (most → least specific):
 *   failed > gate-pending > running(active) > done(completed) > skipped > pending
 */
export function buildRunOverlay(
  run: (PipelineRun & RunStageShape) | RunStageShape | null | undefined,
  graph?: BlueprintGraph | null,
  pendingGates?: PendingGates,
): RunStateOverlay {
  const overlay: RunStateOverlay = {};
  if (!run) return overlay;

  const completed = new Set(run.stages_completed ?? []);
  const skipped = new Set(run.stages_skipped ?? []);
  const failed = new Set(run.stages_failed ?? []);

  // Per-stage wall-clock from the last duration-bearing event per stage.
  const durations: Record<string, number> = {};
  for (const e of run.stage_events ?? []) {
    if (typeof e.duration_ms === "number" && e.duration_ms > 0) {
      durations[e.stage] = e.duration_ms / 1000;
    }
  }

  // Which stage is "active" right now (mid-flight). current_stage from the
  // blueprint engine, falling back to the legacy `stage` field.
  const active = (run.current_stage || run.stage || "").trim();
  const runIsFailed = normalizeRunState(run.state || run.stage) === "failed";

  // Every node the graph declares, plus any stage seen only in the run record
  // (defensive — keeps a stage visible even if the graph is stale).
  const names = new Set<string>();
  for (const n of graph?.nodes ?? []) names.add(n.name);
  for (const s of [...completed, ...skipped, ...failed]) names.add(s);
  if (active) names.add(active);

  const gateNodes = new Map<string, boolean>();
  for (const n of graph?.nodes ?? []) gateNodes.set(n.name, !!n.gate);

  for (const name of names) {
    let state: RunState;
    const isPendingGate =
      gateNodes.get(name) &&
      (pendingGates?.[name] || (name === active && !runIsFailed));

    if (failed.has(name)) state = "failed";
    else if (isPendingGate) state = "gate-pending";
    else if (name === active && !completed.has(name))
      state = runIsFailed ? "failed" : "running";
    else if (completed.has(name)) state = "done";
    else if (skipped.has(name)) state = "skipped";
    else state = "pending";

    overlay[name] = {
      state,
      durationSeconds: durations[name],
    };
  }
  return overlay;
}

// ─────────────────────────────────────────────────────────────────────────
// RunDAG — run-detail consumer (fetches the graph, overlays live state)
// ─────────────────────────────────────────────────────────────────────────

export function RunDAG({
  blueprint,
  run,
  pendingGates,
  onApprove,
  onReject,
}: {
  blueprint: string;
  run: (PipelineRun & RunStageShape) | null;
  pendingGates?: PendingGates;
  onApprove?: (stage: string) => void;
  onReject?: (stage: string) => void;
}) {
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    if (!blueprint) return;
    let cancelled = false;
    setGraph(null);
    setError("");
    api
      .getBlueprintGraph(blueprint)
      .then((g) => !cancelled && setGraph(g))
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [blueprint]);

  const overlay = useMemo(
    () => buildRunOverlay(run, graph, pendingGates),
    [run, graph, pendingGates],
  );

  if (error) {
    return (
      <div className="px-1 py-4 text-[13px]" style={{ color: "var(--ink-muted)" }}>
        Stage graph unavailable for{" "}
        <span className="font-mono">{blueprint || "this run"}</span> — the
        blueprint runtime may be disabled.
      </div>
    );
  }
  if (!graph) {
    return (
      <div
        className="px-1 py-4 text-[13px] animate-pulse"
        style={{ color: "var(--ink-muted)" }}
      >
        Loading {blueprint} stages…
      </div>
    );
  }

  return (
    <BlueprintDAG
      graph={graph}
      overlay={overlay}
      onApprove={onApprove}
      onReject={onReject}
    />
  );
}

// ─────────────────────────────────────────────────────────────────────────
// PipelineFlow — Fleet-home consumer (unchanged API: currentStage + timings)
// ─────────────────────────────────────────────────────────────────────────

interface PipelineFlowProps {
  /** Active stage. Matched against node.name / node.stage. "failed" handled. */
  currentStage: string;
  /** agentKey → seconds, surfaced under the matching node. */
  agentTimings?: Record<string, number>;
  /** Which blueprint to render. Defaults to the ALM pipeline. */
  blueprint?: string;
}

export function PipelineFlow({
  currentStage,
  agentTimings = {},
  blueprint = "alm-pipeline",
}: PipelineFlowProps) {
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setError("");
    api
      .getBlueprintGraph(blueprint)
      .then((g) => !cancelled && setGraph(g))
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [blueprint]);

  // Derive a state overlay purely from the active stage's position in the
  // (topo-ordered) node list: everything before it is done, it is active (or
  // failed), everything after is pending. Durations come from agentTimings.
  const overlay = useMemo<RunStateOverlay>(() => {
    if (!graph) return {};
    const idx = graph.nodes.findIndex(
      (n) => n.name === currentStage || n.stage === currentStage,
    );
    const isFailed = currentStage === "failed";
    const o: RunStateOverlay = {};
    graph.nodes.forEach((n, i) => {
      let state: RunState;
      if (idx < 0) state = "pending";
      else if (i < idx) state = "done";
      else if (i === idx) state = isFailed ? "failed" : "running";
      else state = "pending";
      o[n.name] = {
        state,
        durationSeconds: n.agent ? agentTimings[n.agent] : undefined,
      };
    });
    return o;
  }, [graph, currentStage, agentTimings]);

  if (error) {
    return (
      <div className="w-full px-3 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
        Pipeline flow unavailable for{" "}
        <span className="font-mono">{blueprint}</span> — the blueprint runtime
        may be disabled.
      </div>
    );
  }
  if (!graph) {
    return (
      <div
        className="w-full px-3 py-6 text-sm animate-pulse"
        style={{ color: "var(--ink-muted)" }}
      >
        Loading {blueprint} flow…
      </div>
    );
  }

  return <BlueprintDAG graph={graph} overlay={overlay} />;
}
