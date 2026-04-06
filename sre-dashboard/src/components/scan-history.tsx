"use client";

import { clsx } from "clsx";
import type { ScanRun } from "@/lib/api";

export function ScanHistory({ runs }: { runs: ScanRun[] }) {
  return (
    <div className="space-y-1.5">
      {runs.map((run) => (
        <div
          key={run.id}
          className="flex items-center justify-between p-2.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-primary)]"
        >
          <div className="flex items-center gap-2.5">
            <div className={clsx(
              "w-2 h-2 rounded-full",
              run.status === "completed" ? "bg-green-500" : run.status === "running" ? "bg-blue-500 animate-pulse" : "bg-red-500"
            )} />
            <div>
              <span className="text-xs text-[var(--text-primary)] font-medium">{run.trigger}</span>
              <span className="text-[10px] text-[var(--text-muted)] ml-2">
                {run.apps_checked} apps, {run.incidents_found} incidents
              </span>
            </div>
          </div>
          <span className="text-[10px] text-[var(--text-muted)]">
            {run.started_at ? new Date(run.started_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
          </span>
        </div>
      ))}
    </div>
  );
}
