"use client";

import { RotateCcw } from "lucide-react";

/**
 * Right-rail checkpoint timeline — the "Git & checkpoints" panel from the
 * Cursor screenshot. Each agent step that committed a restore point shows
 * up here; the ↩ button rolls the working tree back to that commit.
 */

export interface Checkpoint {
  sha: string;
  label: string;
  stage?: string;
  ts?: number;
}

function shortTime(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function CheckpointTimeline({
  checkpoints,
  onRollback,
}: {
  checkpoints: Checkpoint[];
  onRollback?: (sha: string) => void;
}) {
  return (
    <div
      className="rounded-lg border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
    >
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold" style={{ color: "var(--text-strong)" }}>
          Checkpoints
        </h3>
        <span className="text-xs" style={{ color: "var(--text-muted)" }}>
          {checkpoints.length}
        </span>
      </div>

      {checkpoints.length === 0 ? (
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          No checkpoints yet. Each agent step that commits a restore point appears here.
        </p>
      ) : (
        <ol className="relative space-y-3">
          {checkpoints.map((cp, i) => (
            <li key={`${cp.sha}-${i}`} className="flex items-start gap-3">
              <span
                className="mt-1 h-2 w-2 shrink-0 rounded-full"
                style={{ background: "var(--accent, #f97316)" }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm" style={{ color: "var(--text-strong)" }}>
                    {cp.label || cp.stage || "checkpoint"}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                      {shortTime(cp.ts)}
                    </span>
                    {onRollback && (
                      <button
                        type="button"
                        title={`Roll back to ${cp.sha.slice(0, 12)}`}
                        onClick={() => onRollback(cp.sha)}
                        className="rounded p-1 transition hover:opacity-80"
                        style={{ color: "var(--text-muted)" }}
                      >
                        <RotateCcw size={13} />
                      </button>
                    )}
                  </div>
                </div>
                <code className="text-[11px]" style={{ color: "var(--text-muted)" }}>
                  {cp.sha.slice(0, 12)}
                </code>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
