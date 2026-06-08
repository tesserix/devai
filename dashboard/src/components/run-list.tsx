"use client";

import type React from "react";
import { useState } from "react";
import Link from "next/link";
import { clsx } from "clsx";
import type { PipelineRun } from "@/lib/api";
import { RunStateBadge, normalizeRunState } from "@/components/run-state-badge";

/**
 * RunList — the recent-runs list.
 *
 * Two presentations from one component:
 *   - "compact" (default): the dense Fleet-home sidebar list, selection-driven
 *     (onSelect), with inline pause/resume/stop/retry controls.
 *   - "rows": the /runs index (DASH-8) — each run is a Link to /runs/<id>,
 *     showing repo · blueprint · source · state · time. No selection state.
 *
 * State colouring is centralised through the shared RunStateBadge /
 * normalizeRunState so a "running" run reads the same here as on the run page.
 */

interface RunRow extends PipelineRun {
  blueprint?: string;
  trigger_type?: string;
  source?: string;
  created_at: string;
}

interface RunListProps {
  runs: RunRow[];
  /** "compact" = selectable sidebar (default). "rows" = linked index rows. */
  variant?: "compact" | "rows";
  selectedRunId?: string;
  onSelect?: (runId: string) => void;
  // Retrigger by run id (server replays the original requirements).
  onRetrigger?: (runId: string) => Promise<void> | void;
  // Pause / resume / stop a running pipeline. Take effect at the next
  // agent boundary (never mid-Claude-call).
  onPause?: (runId: string) => Promise<void> | void;
  onResume?: (runId: string) => Promise<void> | void;
  onStop?: (runId: string) => Promise<void> | void;
  // Delete a run (stops it if live, then removes it). Confirmed before firing.
  onDelete?: (runId: string) => Promise<void> | void;
}

// Stages where the run is considered "in flight" — pause/stop are meaningful.
function isInFlight(stage: string): boolean {
  const s = normalizeRunState(stage);
  return s === "running" || s === "paused" || s === "queued" || s === "gate-pending";
}

export function RunList({
  runs,
  variant = "compact",
  selectedRunId,
  onSelect,
  onRetrigger,
  onPause,
  onResume,
  onStop,
  onDelete,
}: RunListProps) {
  const [busyId, setBusyId] = useState<string | null>(null);

  const handleControl = async (
    runId: string,
    action: ((id: string) => Promise<void> | void) | undefined,
  ) => {
    if (!action || busyId === runId) return;
    setBusyId(runId);
    try {
      await action(runId);
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = (runId: string) => {
    if (!onDelete || busyId === runId) return;
    if (typeof window !== "undefined" && !window.confirm(`Delete run ${runId.slice(0, 8)}? This removes it permanently.`)) {
      return;
    }
    void handleControl(runId, onDelete);
  };

  function controlsFor(run: RunRow) {
    const norm = normalizeRunState(run.stage);
    const isFailed = norm === "failed";
    const isPaused = norm === "paused";
    const inFlight = isInFlight(run.stage);
    const isBusy = busyId === run.run_id;
    return (
      <div className="flex flex-wrap items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
        {isFailed && onRetrigger && (
          <ControlPill
            label="Retry"
            color="accent"
            busy={isBusy}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleControl(run.run_id, onRetrigger);
            }}
          />
        )}
        {inFlight && !isPaused && onPause && (
          <ControlPill
            label="Pause"
            color="warn"
            busy={isBusy}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleControl(run.run_id, onPause);
            }}
          />
        )}
        {isPaused && onResume && (
          <ControlPill
            label="Resume"
            color="ok"
            busy={isBusy}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleControl(run.run_id, onResume);
            }}
          />
        )}
        {(inFlight || isPaused) && onStop && (
          <ControlPill
            label="Stop"
            color="error"
            busy={isBusy}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleControl(run.run_id, onStop);
            }}
          />
        )}
        {onDelete && (
          <ControlPill
            label="Delete"
            color="muted"
            busy={isBusy}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              handleDelete(run.run_id);
            }}
          />
        )}
      </div>
    );
  }

  // ── /runs index rows ────────────────────────────────────────────────
  if (variant === "rows") {
    return (
      <ul className="divide-y" style={{ borderColor: "var(--border-subtle)" }}>
        {runs.map((run) => {
          const agentCount = Object.keys(run.agents || {}).length;
          const source = run.source || run.trigger_type;
          return (
            <li key={run.run_id}>
              <Link
                href={`/runs/${encodeURIComponent(run.run_id)}`}
                className="group flex items-center gap-4 px-4 py-3 transition-colors hover:bg-[var(--surface-muted)]"
              >
                <RunStateBadge state={run.stage} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 min-w-0">
                    <span
                      className="font-medium text-sm truncate"
                      style={{ color: "var(--ink-strong)" }}
                    >
                      {run.repo || "—"}
                    </span>
                    {run.blueprint && (
                      <span
                        className="font-mono text-[11px] px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          background: "var(--surface-muted)",
                          color: "var(--ink-muted)",
                        }}
                      >
                        {run.blueprint}
                      </span>
                    )}
                  </div>
                  <div
                    className="mt-0.5 flex items-center gap-2 text-[11px]"
                    style={{ color: "var(--ink-muted)" }}
                  >
                    <span className="font-mono">{(run.run_id ?? "").slice(0, 12)}</span>
                    {source && (
                      <>
                        <span aria-hidden>·</span>
                        <span>{source}</span>
                      </>
                    )}
                    {agentCount > 0 && (
                      <>
                        <span aria-hidden>·</span>
                        <span>{agentCount} agents</span>
                      </>
                    )}
                  </div>
                </div>
                <span
                  className="text-[11px] shrink-0 tabular-nums"
                  style={{ color: "var(--ink-muted)" }}
                >
                  {formatTime(run.created_at)}
                </span>
                {controlsFor(run)}
              </Link>
            </li>
          );
        })}
      </ul>
    );
  }

  // ── compact selectable sidebar (Fleet home) ─────────────────────────
  return (
    <div className="space-y-1">
      {runs.map((run) => {
        const isSelected = run.run_id === selectedRunId;
        const agentCount = Object.keys(run.agents || {}).length;
        const completedAgents = Object.values(run.agents || {}).filter(
          (a) => a.status === "completed",
        ).length;

        return (
          <button
            key={run.run_id}
            onClick={() => onSelect?.(run.run_id)}
            className={clsx(
              "w-full text-left p-2.5 rounded-md border transition-colors",
            )}
            style={{
              background: isSelected ? "var(--accent-soft-bg-2)" : "var(--surface)",
              borderColor: isSelected ? "var(--accent-soft-bd)" : "var(--border-subtle)",
            }}
          >
            <div className="flex items-center justify-between gap-2">
              <span
                className="text-xs font-mono truncate"
                style={{ color: "var(--ink-muted)" }}
              >
                {(run.run_id ?? "").slice(0, 8)}
              </span>
              <span className="text-xs shrink-0" style={{ color: "var(--ink-muted)" }}>
                {formatTime(run.created_at)}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2">
              <span
                className="text-sm font-medium truncate"
                style={{ color: "var(--ink-strong)" }}
              >
                {run.repo}
              </span>
              <span className="text-xs shrink-0" style={{ color: "var(--ink-muted)" }}>
                {completedAgents}/{agentCount || "?"}
              </span>
            </div>
            {/* State on its own line; controls on a dedicated wrap-friendly row
                below so PAUSE/STOP/DELETE never get clipped in the narrow rail. */}
            <div className="mt-2 space-y-2">
              <RunStateBadge state={run.stage} />
              {controlsFor(run)}
            </div>
          </button>
        );
      })}
    </div>
  );
}

type PillColor = "accent" | "warn" | "ok" | "error" | "muted";

const PILL_CLASS: Record<PillColor, string> = {
  accent: "pill-info",
  warn: "pill-warn",
  ok: "pill-ok",
  error: "pill-error",
  muted: "pill-muted",
};

interface ControlPillProps {
  label: string;
  color: PillColor;
  busy: boolean;
  onClick: (e: React.MouseEvent) => void;
}

function ControlPill({ label, color, busy, onClick }: ControlPillProps) {
  return (
    <span
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onClick(e as unknown as React.MouseEvent);
      }}
      className={clsx(
        "pill",
        PILL_CLASS[color],
        "!px-2 !py-0.5 !text-[10px] font-medium whitespace-nowrap cursor-pointer transition-opacity hover:opacity-80",
        busy && "opacity-50 pointer-events-none",
      )}
      title={label}
      aria-disabled={busy}
    >
      {label}
    </span>
  );
}

function formatTime(ts: string): string {
  if (!ts) return "";
  try {
    const n = parseFloat(ts);
    const d = Number.isFinite(n) ? new Date(n * 1000) : new Date(ts);
    return d.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}
