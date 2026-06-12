"use client";

import { clsx } from "clsx";
import { AGENT_INFO } from "@/lib/constants";

interface AgentCardProps {
  agentKey: string;
  status?: string;
  error?: string;
  timing?: number;
  messageCount?: number;
}

export function AgentCard({ agentKey, status, error, timing, messageCount }: AgentCardProps) {
  const info = AGENT_INFO[agentKey];
  if (!info) return null;

  const isCoordinator = info.role === "coordinator";
  const isRunning = status === "running" || status === "in_progress";

  return (
    <div
      className={clsx(
        "rounded-lg border p-4 transition-all",
        isCoordinator && "border-l-4",
        status === "completed" && "border-green-200 dark:border-green-900 bg-green-50 dark:bg-green-950/30",
        isRunning && "border-indigo-300 dark:border-indigo-700 bg-indigo-50 dark:bg-indigo-950/30 shadow-[0_0_0_1px_rgba(99,102,241,0.3)] animate-pulse",
        status === "failed" && "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/30",
        status === "waiting_approval" && "border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30",
        !status && "border-[var(--border)] bg-[var(--surface)]",
        isCoordinator && !status && "border-l-indigo-400 dark:border-l-indigo-500",
        isCoordinator && status === "completed" && "border-l-green-500",
        isCoordinator && isRunning && "border-l-indigo-500",
      )}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2.5">
          <div
            className={clsx(
              "flex items-center justify-center text-sm font-semibold shrink-0",
              isCoordinator ? "w-10 h-10 rounded-xl" : "w-9 h-9 rounded-lg",
            )}
            style={{ backgroundColor: `${info.color}18`, color: info.color }}
          >
            {info.label.charAt(0)}
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <h3 className="text-sm font-medium text-[var(--ink-strong)]">{info.label}</h3>
              {isCoordinator && (
                <span className="text-xs px-1 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 font-semibold uppercase border border-indigo-100 dark:border-indigo-900">
                  Coord
                </span>
              )}
            </div>
            <p className="text-xs text-[var(--ink-muted)]">{info.provider}</p>
          </div>
        </div>
        <StatusBadge status={status} />
      </div>

      {error && (
        <p className="mt-2 text-xs text-red-600 dark:text-red-400 truncate">{error}</p>
      )}

      <div className="mt-3 flex items-center gap-3 text-xs text-[var(--ink-muted)]">
        {timing !== undefined && (
          <span>{timing.toFixed(1)}s</span>
        )}
        {messageCount !== undefined && messageCount > 0 && (
          <span className="flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 dark:bg-indigo-500" />
            {messageCount} msg{messageCount > 1 ? "s" : ""}
          </span>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string }) {
  if (!status) {
    return (
      <span className="text-xs px-2 py-0.5 rounded-md bg-[var(--surface-hover)] text-[var(--ink-muted)]">
        Idle
      </span>
    );
  }

  const isRunning = status === "running" || status === "in_progress";

  const styles: Record<string, string> = {
    completed: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-400 border border-green-200 dark:border-green-900",
    running: "bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400 border border-indigo-200 dark:border-indigo-900",
    in_progress: "bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400 border border-indigo-200 dark:border-indigo-900",
    failed: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-900",
    waiting_approval: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-900",
  };

  const labels: Record<string, string> = {
    completed: "Done",
    running: "Running",
    in_progress: "Running",
    failed: "Failed",
    waiting_approval: "Awaiting",
  };

  return (
    <span className={clsx("text-xs px-2 py-0.5 rounded-md font-medium flex items-center gap-1.5", styles[status] || styles.running)}>
      {isRunning && (
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-indigo-500" />
        </span>
      )}
      {labels[status] || status}
    </span>
  );
}
