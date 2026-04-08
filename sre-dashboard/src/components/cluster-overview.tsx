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
              hasCritical
                ? "border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/5"
                : hasIssues
                ? "border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/5"
                : "border-emerald-200 dark:border-emerald-500/20 bg-emerald-50 dark:bg-emerald-500/5"
            )}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className={clsx(
                  "w-3 h-3 rounded-full",
                  hasCritical ? "bg-red-500 animate-pulse" : hasIssues ? "bg-amber-500" : "bg-emerald-500"
                )} />
                <h3 className="text-sm font-bold text-gray-900 dark:text-gray-100">{c.cluster_name}</h3>
              </div>
              <span className="text-xs text-gray-400 dark:text-gray-500">{c.total_apps} apps</span>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-3">
              <div>
                <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">{c.open_incidents}</p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Open</p>
              </div>
              <div>
                <p className={clsx(
                  "text-2xl font-bold",
                  c.critical_incidents > 0
                    ? "text-red-600 dark:text-red-400"
                    : "text-gray-900 dark:text-gray-100"
                )}>
                  {c.critical_incidents}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Critical</p>
              </div>
              <div>
                <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                  {c.latest_daily_cost ? `$${c.latest_daily_cost.toFixed(0)}` : "—"}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Daily Cost</p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
