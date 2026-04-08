"use client";

import { clsx } from "clsx";
import type { ScanRun } from "@/lib/api";

export function ScanHistory({ runs }: { runs: ScanRun[] }) {
  if (runs.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 dark:text-slate-500">
        <p className="text-sm">No scan runs yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {runs.map((run) => (
        <div
          key={run.id}
          className="flex items-center justify-between p-2.5 rounded-lg bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800"
        >
          <div className="flex items-center gap-2.5">
            <div className={clsx(
              "w-2 h-2 rounded-full flex-shrink-0",
              run.status === "completed"
                ? "bg-emerald-500"
                : run.status === "running"
                ? "bg-blue-500 animate-pulse"
                : "bg-red-500"
            )} />
            <div>
              <span className="text-xs text-gray-900 dark:text-slate-100 font-medium">{run.trigger}</span>
              <span className="text-[10px] text-gray-400 dark:text-slate-500 ml-2">
                {run.apps_checked} apps, {run.incidents_found} incidents
              </span>
            </div>
          </div>
          <span className="text-[10px] text-gray-400 dark:text-slate-500">
            {run.started_at ? new Date(run.started_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
          </span>
        </div>
      ))}
    </div>
  );
}
