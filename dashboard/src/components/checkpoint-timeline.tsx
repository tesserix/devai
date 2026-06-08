"use client";

import { RotateCcw } from "lucide-react";

/**
 * Right-rail checkpoint timeline — the "Git & checkpoints" panel. Each agent
 * step that committed a restore point shows up here; the ↩ button rolls the
 * working tree back to that commit.
 *
 * Rollback honesty (DASH-10): rollback only does something when the backend
 * worktree consumer is wired. The parent can pass `rollbackDisabledReason` to
 * render the control as disabled with an explanatory tooltip instead of having
 * it silently no-op — or omit `onRollback` entirely to hide it.
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
  /** When set, the ↩ button renders disabled with this tooltip (honest no-op). */
  rollbackDisabledReason,
}: {
  checkpoints: Checkpoint[];
  onRollback?: (sha: string) => void;
  rollbackDisabledReason?: string;
}) {
  const showRollback = !!onRollback || !!rollbackDisabledReason;
  const disabled = !onRollback;

  return (
    <div
      className="rounded-lg border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
    >
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
          Checkpoints
        </h3>
        <span className="text-xs tabular-nums" style={{ color: "var(--ink-muted)" }}>
          {checkpoints.length}
        </span>
      </div>

      {checkpoints.length === 0 ? (
        <p className="text-xs leading-relaxed" style={{ color: "var(--ink-muted)" }}>
          No checkpoints yet. Each agent step that commits a restore point appears here.
        </p>
      ) : (
        <ol className="relative space-y-3">
          {checkpoints.map((cp, i) => (
            <li key={`${cp.sha}-${i}`} className="flex items-start gap-3">
              <span
                className="mt-1 h-2 w-2 shrink-0 rounded-full"
                style={{ background: "var(--accent)" }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm" style={{ color: "var(--ink-strong)" }}>
                    {cp.label || cp.stage || "checkpoint"}
                  </span>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="text-xs tabular-nums" style={{ color: "var(--ink-muted)" }}>
                      {shortTime(cp.ts)}
                    </span>
                    {showRollback && (
                      <button
                        type="button"
                        disabled={disabled}
                        title={
                          disabled
                            ? rollbackDisabledReason || "Rollback unavailable"
                            : `Roll back to ${cp.sha.slice(0, 12)}`
                        }
                        onClick={() => onRollback?.(cp.sha)}
                        className="btn-ghost p-1 transition"
                        style={disabled ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
                        aria-disabled={disabled}
                      >
                        <RotateCcw size={13} />
                      </button>
                    )}
                  </div>
                </div>
                <code className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
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
