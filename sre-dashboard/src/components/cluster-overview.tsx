"use client";

import { clsx } from "clsx";
import type { ClusterHealth } from "@/lib/api";

export function ClusterOverview({ clusters }: { clusters: ClusterHealth[] }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {clusters.map((c) => {
        const hasIssues = c.open_incidents > 0;
        const hasCritical = c.critical_incidents > 0;

        return (
          <div
            key={c.cluster_id}
            className={clsx(
              "p-5 rounded-xl border",
              hasCritical && "border-red-500/30 bg-red-500/5",
              hasIssues && !hasCritical && "border-amber-500/30 bg-amber-500/5",
              !hasIssues && "border-green-500/20 bg-green-500/5"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className={clsx(
                  "w-3 h-3 rounded-full",
                  hasCritical ? "bg-red-500 animate-pulse" : hasIssues ? "bg-amber-500" : "bg-green-500"
                )} />
                <h3 className="text-sm font-bold text-[var(--text-primary)]">{c.cluster_name}</h3>
              </div>
              <span className="text-xs text-[var(--text-muted)]">{c.total_apps} apps</span>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-3">
              <div>
                <p className="text-2xl font-bold text-[var(--text-primary)]">{c.open_incidents}</p>
                <p className="text-[10px] text-[var(--text-muted)] mt-0.5">Open</p>
              </div>
              <div>
                <p className={clsx("text-2xl font-bold", c.critical_incidents > 0 ? "text-red-400" : "text-[var(--text-primary)]")}>
                  {c.critical_incidents}
                </p>
                <p className="text-[10px] text-[var(--text-muted)] mt-0.5">Critical</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-[var(--text-primary)]">
                  {c.latest_daily_cost ? `$${c.latest_daily_cost.toFixed(0)}` : "—"}
                </p>
                <p className="text-[10px] text-[var(--text-muted)] mt-0.5">Daily Cost</p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
