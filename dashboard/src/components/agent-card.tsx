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

  return (
    <div
      className={clsx(
        "rounded-lg border p-4 transition-all",
        status === "completed" && "border-green-500/30 bg-green-500/5",
        status === "running" && "border-blue-500/30 bg-blue-500/5 animate-pulse",
        status === "failed" && "border-red-500/30 bg-red-500/5",
        status === "waiting_approval" && "border-amber-500/30 bg-amber-500/5",
        !status && "border-[var(--border-primary)] bg-[var(--bg-secondary)]"
      )}
    >
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2.5">
          <span className="text-xl">{info.icon}</span>
          <div>
            <h3 className="text-sm font-semibold text-[var(--text-primary)]">{info.label}</h3>
            <p className="text-xs text-[var(--text-muted)]">{info.provider}</p>
          </div>
        </div>
        <StatusBadge status={status} />
      </div>

      {error && (
        <p className="mt-2 text-xs text-red-400 truncate">{error}</p>
      )}

      <div className="mt-3 flex items-center gap-3 text-xs text-[var(--text-muted)]">
        {timing !== undefined && (
          <span>{timing.toFixed(1)}s</span>
        )}
        {messageCount !== undefined && messageCount > 0 && (
          <span>{messageCount} msg{messageCount > 1 ? "s" : ""}</span>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string }) {
  if (!status) return <span className="text-[10px] px-2 py-0.5 rounded-full bg-[var(--bg-tertiary)] text-[var(--text-muted)]">Idle</span>;

  const styles: Record<string, string> = {
    completed: "bg-green-500/15 text-green-400",
    running: "bg-blue-500/15 text-blue-400",
    failed: "bg-red-500/15 text-red-400",
    waiting_approval: "bg-amber-500/15 text-amber-400",
  };

  const labels: Record<string, string> = {
    completed: "Done",
    running: "Running",
    failed: "Failed",
    waiting_approval: "Approval",
  };

  return (
    <span className={clsx("text-[10px] px-2 py-0.5 rounded-full font-medium", styles[status] || styles.running)}>
      {labels[status] || status}
    </span>
  );
}
