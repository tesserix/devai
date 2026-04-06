"use client";

import { clsx } from "clsx";
import type { PipelineRun } from "@/lib/api";

interface RunListProps {
  runs: PipelineRun[];
  selectedRunId?: string;
  onSelect: (runId: string) => void;
}

const STAGE_COLORS: Record<string, string> = {
  triggered: "bg-gray-500",
  requirements_analyzed: "bg-cyan-500",
  epic_created: "bg-purple-500",
  stories_created: "bg-blue-500",
  plan_created: "bg-indigo-500",
  code_implemented: "bg-amber-500",
  code_reviewed: "bg-orange-500",
  build_monitoring: "bg-yellow-500",
  tests_complete: "bg-lime-500",
  deploying: "bg-emerald-500",
  deployed: "bg-green-500",
  done: "bg-green-600",
  failed: "bg-red-500",
};

export function RunList({ runs, selectedRunId, onSelect }: RunListProps) {
  if (runs.length === 0) {
    return (
      <div className="text-center py-12 text-[var(--text-muted)]">
        <p className="text-sm">No pipeline runs yet</p>
        <p className="text-xs mt-1">Trigger a run from the CLI or webhook</p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
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
              "w-full text-left p-3 rounded-lg border transition-all",
              isSelected
                ? "border-blue-500/40 bg-blue-500/10"
                : "border-[var(--border-primary)] bg-[var(--bg-secondary)] hover:border-[var(--border-secondary)]"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className={clsx("w-2 h-2 rounded-full", STAGE_COLORS[run.stage] || "bg-gray-500")} />
                <span className="text-xs font-mono text-[var(--text-secondary)]">
                  {run.run_id.slice(0, 10)}...
                </span>
              </div>
              <span className="text-[10px] text-[var(--text-muted)]">
                {formatTime(run.created_at)}
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between">
              <span className="text-xs text-[var(--text-primary)] font-medium">{run.repo}</span>
              <span className="text-[10px] text-[var(--text-muted)]">
                {completedAgents}/{agentCount || "?"} agents
              </span>
            </div>
            <div className="mt-1">
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
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
