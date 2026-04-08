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

  return (
    <div
      className={clsx(
        "rounded-lg border p-4 transition-all",
        isCoordinator && "border-l-4",
        status === "completed" && "border-green-200 dark:border-green-900 bg-green-50 dark:bg-green-950/30",
        status === "running" && "border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-950/30",
        status === "failed" && "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/30",
        status === "waiting_approval" && "border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30",
        !status && "border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800",
        isCoordinator && !status && "border-l-indigo-400 dark:border-l-indigo-500",
        isCoordinator && status === "completed" && "border-l-green-500",
        isCoordinator && status === "running" && "border-l-indigo-500",
        status === "running" && "animate-pulse"
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
              <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">{info.label}</h3>
              {isCoordinator && (
                <span className="text-xs px-1 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 font-semibold uppercase border border-indigo-100 dark:border-indigo-900">
                  Coord
                </span>
              )}
            </div>
            <p className="text-xs text-gray-400 dark:text-gray-500">{info.provider}</p>
          </div>
        </div>
        <StatusBadge status={status} />
      </div>

      {error && (
        <p className="mt-2 text-xs text-red-600 dark:text-red-400 truncate">{error}</p>
      )}

      <div className="mt-3 flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500">
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
      <span className="text-xs px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400">
        Idle
      </span>
    );
  }

  const styles: Record<string, string> = {
    completed: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-400 border border-green-200 dark:border-green-900",
    running: "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-400 border border-blue-200 dark:border-blue-900",
    failed: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-900",
    waiting_approval: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-900",
  };

  const labels: Record<string, string> = {
    completed: "Done",
    running: "Running",
    failed: "Failed",
    waiting_approval: "Awaiting",
  };

  return (
    <span className={clsx("text-xs px-2 py-0.5 rounded-md font-medium", styles[status] || styles.running)}>
      {labels[status] || status}
    </span>
  );
}
