"use client";

import { clsx } from "clsx";
import type { Incident } from "@/lib/api";

const SEVERITY_STYLES: Record<string, { bg: string; text: string; dot: string }> = {
  critical: { bg: "bg-red-500/10", text: "text-red-400", dot: "bg-red-500" },
  high: { bg: "bg-orange-500/10", text: "text-orange-400", dot: "bg-orange-500" },
  medium: { bg: "bg-amber-500/10", text: "text-amber-400", dot: "bg-amber-500" },
  low: { bg: "bg-blue-500/10", text: "text-blue-400", dot: "bg-blue-400" },
  info: { bg: "bg-gray-500/10", text: "text-gray-400", dot: "bg-gray-400" },
};

const CATEGORY_ICONS: Record<string, string> = {
  performance: "⚡",
  cost: "💰",
  availability: "🔴",
  security: "🛡️",
  capacity: "📊",
  error: "❌",
};

export function IncidentFeed({ incidents, onSelect }: { incidents: Incident[]; onSelect: (id: string) => void }) {
  if (incidents.length === 0) {
    return (
      <div className="text-center py-12 text-[var(--text-muted)]">
        <p className="text-lg">All Clear</p>
        <p className="text-sm mt-1">No open incidents</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {incidents.map((inc) => {
        const sev = SEVERITY_STYLES[inc.severity] || SEVERITY_STYLES.info;
        const icon = CATEGORY_ICONS[inc.category] || "⚠️";

        return (
          <button
            key={inc.id}
            onClick={() => onSelect(inc.id)}
            className={clsx(
              "w-full text-left p-3.5 rounded-lg border transition-all hover:border-[var(--border-secondary)]",
              sev.bg, "border-[var(--border-primary)]"
            )}
          >
            <div className="flex items-start gap-3">
              <span className="text-lg mt-0.5">{icon}</span>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <div className={clsx("w-2 h-2 rounded-full", sev.dot)} />
                  <span className={clsx("text-[10px] font-bold uppercase tracking-wider", sev.text)}>
                    {inc.severity}
                  </span>
                  <span className="text-[10px] text-[var(--text-muted)]">
                    {inc.category}
                  </span>
                </div>
                <p className="text-sm font-medium text-[var(--text-primary)] mt-1 truncate">{inc.title}</p>
                <div className="flex items-center gap-3 mt-1.5 text-[10px] text-[var(--text-muted)]">
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
