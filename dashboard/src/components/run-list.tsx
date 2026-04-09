"use client";

import type React from "react";
import { useState } from "react";
import { clsx } from "clsx";
import type { PipelineRun } from "@/lib/api";

interface RunListProps {
  runs: PipelineRun[];
  selectedRunId?: string;
  onSelect: (runId: string) => void;
  // Retrigger by run id (server replays the original requirements).
  onRetrigger?: (runId: string) => Promise<void> | void;
  // Pause / resume / stop a running pipeline. Take effect at the next
  // agent boundary (never mid-Claude-call).
  onPause?: (runId: string) => Promise<void> | void;
  onResume?: (runId: string) => Promise<void> | void;
  onStop?: (runId: string) => Promise<void> | void;
}

const STAGE_DOT: Record<string, string> = {
  triggered: "bg-gray-400",
  requirements_analyzed: "bg-gray-500",
  epic_created: "bg-indigo-500",
  stories_created: "bg-blue-500",
  plan_created: "bg-indigo-600",
  code_implemented: "bg-amber-500",
  code_reviewed: "bg-orange-500",
  build_monitoring: "bg-yellow-500",
  tests_complete: "bg-lime-600",
  deploying: "bg-teal-500",
  deployed: "bg-green-600",
  done: "bg-green-700",
  failed: "bg-red-600",
  cancelled: "bg-gray-500",
  paused: "bg-amber-400",
};

// Stages where the run is considered "in flight" — pause/stop are
// meaningful. Anything in this set shows the pause + stop controls.
const ACTIVE_STAGES = new Set([
  "triggered",
  "requirements_analyzed",
  "epic_created",
  "stories_created",
  "plan_created",
  "code_implemented",
  "code_reviewed",
  "build_monitoring",
  "tests_complete",
  "deploying",
  "running",
  "paused",
]);

export function RunList({
  runs,
  selectedRunId,
  onSelect,
  onRetrigger,
  onPause,
  onResume,
  onStop,
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

  if (runs.length === 0) {
    return (
      <div className="text-center py-10 text-gray-400 dark:text-gray-500">
        <p className="text-sm">No pipeline runs yet</p>
        <p className="text-xs mt-1">Trigger a run from the CLI or webhook</p>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {runs.map((run) => {
        const isSelected = run.run_id === selectedRunId;
        const agentCount = Object.keys(run.agents || {}).length;
        const completedAgents = Object.values(run.agents || {}).filter(
          (a) => a.status === "completed"
        ).length;
        const isFailed = run.stage === "failed";
        const isPaused = run.stage === "paused";
        const isCancelled = run.stage === "cancelled";
        const isActive = ACTIVE_STAGES.has(run.stage) && !isFailed && !isCancelled;
        const isBusy = busyId === run.run_id;

        return (
          <button
            key={run.run_id}
            onClick={() => onSelect(run.run_id)}
            className={clsx(
              "w-full text-left p-2.5 rounded-md border transition-all",
              isSelected
                ? "border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-950/40"
                : "border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700/50"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className={clsx("w-1.5 h-1.5 rounded-full shrink-0", STAGE_DOT[run.stage] || "bg-gray-400")} />
                <span className="text-xs font-mono text-gray-500 dark:text-gray-500">
                  {run.run_id.slice(0, 8)}
                </span>
              </div>
              <span className="text-xs text-gray-400 dark:text-gray-500">
                {formatTime(run.created_at)}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate max-w-[140px]">
                {run.repo}
              </span>
              <span className="text-xs text-gray-400 dark:text-gray-500 shrink-0 ml-1">
                {completedAgents}/{agentCount || "?"} agents
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between">
              <span className={clsx(
                "text-xs px-1.5 py-0.5 rounded",
                isFailed
                  ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400"
                  : "bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400"
              )}>
                {run.stage.replace(/_/g, " ")}
              </span>
              <div className="flex items-center gap-1">
                {isFailed && onRetrigger && (
                  <ControlPill
                    label="Retry"
                    color="indigo"
                    busy={isBusy}
                    onClick={async (e) => {
                      e.stopPropagation();
                      await handleControl(run.run_id, onRetrigger);
                    }}
                  />
                )}
                {isActive && !isPaused && onPause && (
                  <ControlPill
                    label="Pause"
                    color="amber"
                    busy={isBusy}
                    onClick={async (e) => {
                      e.stopPropagation();
                      await handleControl(run.run_id, onPause);
                    }}
                  />
                )}
                {isPaused && onResume && (
                  <ControlPill
                    label="Resume"
                    color="emerald"
                    busy={isBusy}
                    onClick={async (e) => {
                      e.stopPropagation();
                      await handleControl(run.run_id, onResume);
                    }}
                  />
                )}
                {(isActive || isPaused) && onStop && (
                  <ControlPill
                    label="Stop"
                    color="red"
                    busy={isBusy}
                    onClick={async (e) => {
                      e.stopPropagation();
                      await handleControl(run.run_id, onStop);
                    }}
                  />
                )}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

type PillColor = "indigo" | "amber" | "emerald" | "red";

const PILL_CLASSES: Record<PillColor, string> = {
  indigo:
    "bg-indigo-100 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-200 dark:hover:bg-indigo-800/40",
  amber:
    "bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 hover:bg-amber-200 dark:hover:bg-amber-800/40",
  emerald:
    "bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-200 dark:hover:bg-emerald-800/40",
  red:
    "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 hover:bg-red-200 dark:hover:bg-red-800/40",
};

interface ControlPillProps {
  label: string;
  color: PillColor;
  busy: boolean;
  onClick: (e: React.MouseEvent) => Promise<void> | void;
}

function ControlPill({ label, color, busy, onClick }: ControlPillProps) {
  return (
    <span
      role="button"
      onClick={onClick}
      className={clsx(
        "text-xs px-2 py-0.5 rounded transition-colors",
        busy ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
        PILL_CLASSES[color],
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
    const d = new Date(parseFloat(ts) * 1000);
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
