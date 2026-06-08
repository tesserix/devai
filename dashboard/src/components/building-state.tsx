"use client";

/**
 * BuildingState — the legible "Building preview…" surface (DASH-13).
 *
 * A preview is not instant: it clones the repo, installs dependencies, then
 * boots a dev server (and optionally a backend + seeded Postgres). That takes
 * minutes, during which the URL serves a raw 503 / "no healthy upstream". This
 * component stands in front of the iframe until the preview is actually ready,
 * showing the clone → install → boot phases and per-container health dots.
 *
 * It is pure presentational: the parent owns polling (preview.get / verify) and
 * passes the current status + the latest verify diagnoses down. No fetching here.
 *
 * Used by BOTH preview entry paths (user-started and app-scaffold pipeline).
 */

import { clsx } from "clsx";
import { AlertTriangle, Loader2, RefreshCw, Square } from "lucide-react";
import type { PreviewDiagnosis, PreviewSession } from "@/lib/api";

/** Coarse bring-up phase, derived by the parent from elapsed time / events. */
export type BuildPhase = "clone" | "install" | "boot";

const PHASES: { key: BuildPhase; label: string; hint: string }[] = [
  { key: "clone", label: "Clone", hint: "Fetching the repository" },
  { key: "install", label: "Install", hint: "Installing dependencies" },
  { key: "boot", label: "Boot", hint: "Starting the dev server" },
];

// Map a single container diagnosis class onto a dot modifier + label.
function containerDot(d: PreviewDiagnosis): { cls: string; label: string; spin: boolean } {
  switch (d.class) {
    case "healthy":
      return { cls: "dot-ok", label: "healthy", spin: false };
    case "pending":
      return { cls: "dot-running", label: "starting", spin: true };
    case "oom":
    case "crashloop":
    case "install_failed":
    case "image_pull":
      return { cls: "dot-error", label: d.class.replace(/_/g, " "), spin: false };
    case "port_mismatch":
    case "cors":
      return { cls: "dot-warn", label: d.class.replace(/_/g, " "), spin: false };
    default:
      return { cls: "dot-muted", label: "unknown", spin: false };
  }
}

export function BuildingState({
  session,
  phase = "install",
  diagnoses = [],
  onRefresh,
  onStop,
  refreshing = false,
  /** Free-text note (e.g. "this can take a few minutes"). */
  note,
}: {
  session: Pick<PreviewSession, "repo" | "ref" | "status"> & { session_id?: string };
  /** Best-effort current phase; drives the stepper highlight. */
  phase?: BuildPhase;
  /** Latest per-container diagnoses from preview.verify (optional). */
  diagnoses?: PreviewDiagnosis[];
  onRefresh?: () => void;
  onStop?: () => void;
  refreshing?: boolean;
  note?: string;
}) {
  const isFailed = session.status === "failed";
  const isDegraded = session.status === "degraded";
  const activeIdx = PHASES.findIndex((p) => p.key === phase);

  return (
    <div
      className="flex h-full min-h-0 w-full flex-col items-center justify-center gap-5 p-8 text-center"
      style={{ background: "var(--surface)" }}
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-col items-center gap-3 max-w-md">
        <span
          className="inline-flex items-center justify-center w-11 h-11 rounded-xl"
          style={{
            background: isFailed ? "var(--error-soft-bg)" : "var(--surface-muted)",
            border: `1px solid ${isFailed ? "var(--error-soft-bd)" : "var(--border-subtle)"}`,
          }}
        >
          {isFailed ? (
            <AlertTriangle className="w-5 h-5" style={{ color: "var(--error)" }} aria-hidden />
          ) : (
            <Loader2 className="w-5 h-5 animate-spin" style={{ color: "var(--accent)" }} aria-hidden />
          )}
        </span>

        <div>
          <h3 className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
            {isFailed
              ? "Preview failed to start"
              : isDegraded
                ? "Preview is degraded"
                : "Building preview…"}
          </h3>
          <p className="mt-1 text-[13px]" style={{ color: "var(--ink-muted)" }}>
            <span className="font-mono">{session.repo}</span>
            <span style={{ color: "var(--ink-disabled)" }}> @ {session.ref}</span>
          </p>
        </div>

        {/* clone → install → boot stepper */}
        {!isFailed && (
          <div className="flex items-center gap-2 mt-1">
            {PHASES.map((p, i) => {
              const done = i < activeIdx;
              const current = i === activeIdx;
              return (
                <div key={p.key} className="flex items-center gap-2">
                  <span
                    className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-medium"
                    style={{
                      background: current
                        ? "var(--accent-soft-bg-2)"
                        : done
                          ? "var(--ok-soft-bg)"
                          : "var(--surface-muted)",
                      color: current
                        ? "var(--accent-soft-ink)"
                        : done
                          ? "var(--ok-ink)"
                          : "var(--ink-muted)",
                      border: "1px solid var(--border-subtle)",
                    }}
                    title={p.hint}
                  >
                    <span
                      className={clsx(
                        "dot",
                        done ? "dot-ok" : current ? "dot-running animate-pulse" : "dot-muted",
                      )}
                      aria-hidden
                    />
                    {p.label}
                  </span>
                  {i < PHASES.length - 1 && (
                    <span style={{ color: "var(--ink-disabled)" }} aria-hidden>
                      →
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}

        <p className="text-[12px] leading-relaxed" style={{ color: "var(--ink-muted)" }}>
          {note ??
            (isFailed
              ? "Check the run's timeline for the failing container, then retry."
              : "Cloning, installing and booting can take a few minutes. This view refreshes automatically.")}
        </p>
      </div>

      {/* Per-container health dots */}
      {diagnoses.length > 0 && (
        <div
          className="panel-muted w-full max-w-md p-3 text-left"
          aria-label="Container health"
        >
          <div className="label-eyebrow mb-2">Containers</div>
          <ul className="space-y-1.5">
            {diagnoses.map((d, i) => {
              const dot = containerDot(d);
              return (
                <li key={`${d.container}-${i}`} className="flex items-center gap-2 text-[12px]">
                  <span className={clsx("dot", dot.cls, dot.spin && "animate-pulse")} aria-hidden />
                  <span className="font-mono" style={{ color: "var(--ink)" }}>
                    {d.container}
                  </span>
                  <span style={{ color: "var(--ink-muted)" }}>{dot.label}</span>
                  {d.detail && (
                    <span className="truncate" style={{ color: "var(--ink-disabled)" }} title={d.detail}>
                      · {d.detail}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Controls */}
      {(onRefresh || onStop) && (
        <div className="flex items-center gap-2">
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              disabled={refreshing}
              className="btn-secondary !py-1.5 !text-sm"
            >
              <RefreshCw className={clsx("w-3.5 h-3.5", refreshing && "animate-spin")} />
              {refreshing ? "Checking…" : "Check status"}
            </button>
          )}
          {onStop && (
            <button type="button" onClick={onStop} className="btn-ghost !py-1.5 !text-sm">
              <Square className="w-3.5 h-3.5" />
              Stop preview
            </button>
          )}
        </div>
      )}
    </div>
  );
}
