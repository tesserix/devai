"use client";

import { clsx } from "clsx";
import type { PipelineRun } from "@/lib/api";

interface RunListProps {
  runs: PipelineRun[];
  selectedRunId?: string;
  onSelect: (runId: string) => void;
}

const STAGE_DOT: Record<string, string> = {
  triggered: "bg-gray-400",
  requirements_analyzed: "bg-slate-500",
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

export function RunList({ runs, selectedRunId, onSelect }: RunListProps) {
  if (runs.length === 0) {
    return (
      <div className="text-center py-10 text-gray-400 dark:text-slate-500">
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

        return (
          <button
            key={run.run_id}
            onClick={() => onSelect(run.run_id)}
            className={clsx(
              "w-full text-left p-2.5 rounded-md border transition-all",
              isSelected
                ? "border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-950/40"
                : "border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900 hover:border-gray-300 dark:hover:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-800/50"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className={clsx("w-1.5 h-1.5 rounded-full shrink-0", STAGE_DOT[run.stage] || "bg-gray-400")} />
                <span className="text-[10px] font-mono text-gray-500 dark:text-slate-500">
                  {run.run_id.slice(0, 8)}
                </span>
              </div>
              <span className="text-[10px] text-gray-400 dark:text-slate-500">
                {formatTime(run.created_at)}
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-xs font-medium text-gray-800 dark:text-slate-200 truncate max-w-[140px]">
                {run.repo}
              </span>
              <span className="text-[10px] text-gray-400 dark:text-slate-500 shrink-0 ml-1">
                {completedAgents}/{agentCount || "?"} agents
              </span>
            </div>
            <div className="mt-1.5">
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-slate-800 text-gray-500 dark:text-slate-400">
                {run.stage.replace(/_/g, " ")}
              </span>
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
