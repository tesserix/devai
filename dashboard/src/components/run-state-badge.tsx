"use client";

/**
 * RunStateBadge — one consistent status pill for runs AND stages.
 *
 * The whole dashboard previously coloured states ad-hoc (one map in run-list,
 * another in chat-panel, more inline). This centralises the vocabulary and the
 * token mapping so a "running" run looks the same everywhere.
 *
 * It maps the many raw backend strings (running / in_progress / active /
 * completed / merged / done / failed / error / paused / cancelled / queued /
 * pending / skipped / gate-pending …) onto a small canonical set, then renders
 * a `pill pill-*` from globals.css. Pure presentational.
 */

import { clsx } from "clsx";

/** The canonical states a run or stage can be in, after normalization. */
export type RunState =
  | "pending"
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | "paused"
  | "cancelled"
  | "gate-pending";

const LABELS: Record<RunState, string> = {
  pending: "Pending",
  queued: "Queued",
  running: "Running",
  done: "Done",
  failed: "Failed",
  skipped: "Skipped",
  paused: "Paused",
  cancelled: "Cancelled",
  "gate-pending": "Awaiting approval",
};

// Maps a canonical state to one of the shared pill modifier classes.
const PILL_CLASS: Record<RunState, string> = {
  pending: "pill-muted",
  queued: "pill-muted",
  running: "pill-info",
  done: "pill-ok",
  failed: "pill-error",
  skipped: "pill-muted",
  paused: "pill-warn",
  cancelled: "pill-muted",
  "gate-pending": "pill-warn",
};

// Maps a canonical state to a dot modifier (for the small leading indicator).
const DOT_CLASS: Record<RunState, string> = {
  pending: "dot-muted",
  queued: "dot-muted",
  running: "dot-running",
  done: "dot-ok",
  failed: "dot-error",
  skipped: "dot-muted",
  paused: "dot-warn",
  cancelled: "dot-muted",
  "gate-pending": "dot-warn",
};

/**
 * Normalize any raw run/stage string into a canonical RunState.
 * Exported so non-badge callers (the DAG overlay, run-list) share one mapping.
 */
export function normalizeRunState(raw: string | null | undefined): RunState {
  const s = (raw ?? "").toLowerCase().trim();
  if (!s) return "pending";
  if (s === "gate" || s === "gate-pending" || s === "awaiting_approval" || s === "waiting_approval")
    return "gate-pending";
  if (s === "paused" || s === "blocked") return "paused";
  if (s === "cancelled" || s === "canceled" || s === "stopped") return "cancelled";
  if (s === "skipped" || s === "skip") return "skipped";
  if (s === "queued" || s === "dispatched" || s === "scheduled") return "queued";
  if (
    s === "pending" ||
    s === "idle" ||
    s === "triggered" ||
    s === "created" ||
    s === "not_started"
  )
    return "pending";
  if (s.includes("fail") || s.includes("error") || s === "rejected") return "failed";
  if (s === "done" || s.includes("complet") || s === "merged" || s === "deployed" || s === "succeeded" || s === "approved")
    return "done";
  if (s.includes("running") || s === "active" || s === "in_progress" || s === "in-progress" || s === "started")
    return "running";
  // Any mid-pipeline stage name (e.g. code_implemented) means the run is moving.
  return "running";
}

export function RunStateBadge({
  state,
  /** Show the leading dot (default true). */
  dot = true,
  /** Override the displayed label (defaults to the canonical label). */
  label,
  className,
}: {
  /** A canonical RunState OR any raw backend string — normalized internally. */
  state: RunState | string;
  dot?: boolean;
  label?: string;
  className?: string;
}) {
  const canonical = (LABELS as Record<string, string>)[state]
    ? (state as RunState)
    : normalizeRunState(String(state));
  const text = label ?? LABELS[canonical];

  return (
    <span className={clsx("pill", PILL_CLASS[canonical], className)}>
      {dot && (
        <span
          className={clsx(
            "dot",
            DOT_CLASS[canonical],
            canonical === "running" && "animate-pulse",
          )}
          aria-hidden
        />
      )}
      {text}
    </span>
  );
}
