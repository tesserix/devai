"use client";

import { useState } from "react";
import { clsx } from "clsx";
import type { PipelineRun } from "@/lib/api";

interface RunListProps {
  runs: PipelineRun[];
  selectedRunId?: string;
  onSelect: (runId: string) => void;
  onRetrigger?: (repo: string) => Promise<void> | void;
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
};

export function RunList({ runs, selectedRunId, onSelect, onRetrigger }: RunListProps) {
  const [retriggeringId, setRetriggeringId] = useState<string | null>(null);

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
              {isFailed && onRetrigger && (
                <span
                  role="button"
                  onClick={async (e) => {
                    e.stopPropagation();
                    if (retriggeringId === run.run_id) return;
                    setRetriggeringId(run.run_id);
                    try {
                      await onRetrigger(run.repo);
                    } finally {
                      setRetriggeringId(null);
                    }
                  }}
                  className={clsx(
                    "text-xs px-2 py-0.5 rounded transition-colors",
                    retriggeringId === run.run_id
                      ? "bg-indigo-200 dark:bg-indigo-800/50 text-indigo-400 dark:text-indigo-500 cursor-not-allowed"
                      : "bg-indigo-100 dark:bg-indigo-900/30 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-200 dark:hover:bg-indigo-800/40 cursor-pointer"
                  )}
                  title="Retrigger pipeline for this repo"
                  aria-disabled={retriggeringId === run.run_id}
                >
                  {retriggeringId === run.run_id ? (
                    <span className="flex items-center gap-1">
                      <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                      Retrying…
                    </span>
                  ) : (
                    "Retry"
                  )}
                </span>
              )}
            </div>
          </button>
        );
      })}
    </div>
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
