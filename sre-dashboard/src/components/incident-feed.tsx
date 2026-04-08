"use client";

import { clsx } from "clsx";
import type { Incident } from "@/lib/api";

const SEVERITY_STYLES: Record<string, { border: string; bg: string; text: string; dot: string }> = {
  critical: {
    border: "border-red-200 dark:border-red-500/30",
    bg: "bg-red-50 dark:bg-red-500/10",
    text: "text-red-600 dark:text-red-400",
    dot: "bg-red-500",
  },
  high: {
    border: "border-orange-200 dark:border-orange-500/30",
    bg: "bg-orange-50 dark:bg-orange-500/10",
    text: "text-orange-600 dark:text-orange-400",
    dot: "bg-orange-500",
  },
  medium: {
    border: "border-amber-200 dark:border-amber-500/30",
    bg: "bg-amber-50 dark:bg-amber-500/10",
    text: "text-amber-600 dark:text-amber-400",
    dot: "bg-amber-500",
  },
  low: {
    border: "border-blue-200 dark:border-blue-500/30",
    bg: "bg-blue-50 dark:bg-blue-500/10",
    text: "text-blue-600 dark:text-blue-400",
    dot: "bg-blue-400",
  },
  info: {
    border: "border-gray-200 dark:border-gray-600",
    bg: "bg-gray-50 dark:bg-gray-700/50",
    text: "text-gray-500 dark:text-gray-400",
    dot: "bg-gray-400",
  },
};

const CATEGORY_LABELS: Record<string, string> = {
  performance: "Performance",
  cost: "Cost",
  availability: "Availability",
  security: "Security",
  capacity: "Capacity",
  error: "Error",
};

export function IncidentFeed({ incidents, onSelect }: { incidents: Incident[]; onSelect: (id: string) => void }) {
  if (incidents.length === 0) {
    return (
      <div className="text-center py-12 text-gray-400 dark:text-gray-500">
        <p className="text-lg font-medium">All Clear</p>
        <p className="text-sm mt-1">No open incidents</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {incidents.map((inc) => {
        const sev = SEVERITY_STYLES[inc.severity] || SEVERITY_STYLES.info;
        const categoryLabel = CATEGORY_LABELS[inc.category] || inc.category;

        return (
          <button
            key={inc.id}
            onClick={() => onSelect(inc.id)}
            className={clsx(
              "w-full text-left p-3.5 rounded-lg border transition-all",
              sev.border, sev.bg,
              "hover:brightness-95 dark:hover:brightness-110"
            )}
          >
            <div className="flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <div className={clsx("w-2 h-2 rounded-full flex-shrink-0", sev.dot)} />
                  <span className={clsx("text-sm font-bold uppercase tracking-wider", sev.text)}>
                    {inc.severity}
                  </span>
                  <span className="text-xs text-gray-400 dark:text-gray-500">
                    {categoryLabel}
                  </span>
                </div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100 mt-1 truncate">{inc.title}</p>
                <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-400 dark:text-gray-500">
                  <span>by {inc.detected_by}</span>
                  {inc.scm_issue_number && <span>#{inc.scm_issue_number}</span>}
                  <span>{new Date(inc.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                </div>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
