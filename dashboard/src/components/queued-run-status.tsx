"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, CircleDashed, Clock3, Loader2, Radio, RefreshCw } from "lucide-react";

import type { PipelineRun } from "@/lib/api";
import type { RunEventStreamStatus } from "@/lib/use-run-events";
import { buildQueuedRunView, formatQueueElapsed, runTimestampMs } from "@/lib/queued-run";

type QueuedRun = PipelineRun & {
  updated_at?: string | number;
  started_at?: string | number | null;
  state?: string;
  stage_events?: Array<{
    stage?: string;
    phase?: string;
    message?: string;
    error?: string;
    timestamp?: string | number;
  }>;
  events?: Array<{
    step?: string;
    status?: string;
    detail?: string;
    timestamp?: string | number;
  }>;
};

function checkedLabel(lastCheckedAt: number | null, nowMs: number): string {
  if (lastCheckedAt === null) return "Waiting for the first status check";
  const elapsed = Math.max(0, Math.floor((nowMs - lastCheckedAt) / 1_000));
  if (elapsed < 2) return "Checked just now";
  return `Checked ${formatQueueElapsed(elapsed)} ago`;
}

function eventTime(value: number | null): string {
  if (value === null) return "";
  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function QueuedRunStatus({
  run,
  streamStatus,
  lastCheckedAt,
}: {
  run: QueuedRun;
  streamStatus: RunEventStreamStatus;
  lastCheckedAt: number | null;
}) {
  const [nowMs, setNowMs] = useState(0);

  useEffect(() => {
    const tick = () => setNowMs(Date.now());
    tick();
    const timer = window.setInterval(tick, 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const view = useMemo(() => buildQueuedRunView(run, nowMs), [run, nowMs]);
  const connected = streamStatus === "connected";
  const streamLabel = connected
    ? "Live event stream connected"
    : streamStatus === "connecting"
      ? "Connecting to live updates"
      : "Live stream reconnecting";
  const pollSeconds = connected ? 15 : 4;
  const acceptedAt = runTimestampMs(run.created_at);

  return (
    <section
      className="rounded-lg border overflow-hidden"
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      role="status"
      aria-live="polite"
      aria-label="Run queue status"
    >
      <div className="p-4 sm:p-5">
        <div className="flex items-start gap-3">
          <span
            className="inline-flex w-10 h-10 shrink-0 items-center justify-center rounded-xl"
            style={{ background: "var(--accent-soft-bg-2)", color: "var(--accent)" }}
          >
            <Loader2 className="w-5 h-5 animate-spin" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <h2 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                  Run accepted
                </h2>
                <p className="mt-1 text-[13px]" style={{ color: "var(--ink-soft)" }}>
                  Waiting for an available worker. DevAI will start this run automatically.
                </p>
              </div>
              <span
                className="pill inline-flex items-center gap-1.5 font-mono"
                style={{ color: "var(--accent-soft-ink)" }}
                aria-live="off"
              >
                <Clock3 className="w-3 h-3" aria-hidden />
                Queued for {view.elapsedLabel}
              </span>
            </div>

            <ol className="mt-5 grid grid-cols-3 gap-2" aria-label="Run startup progress">
              <QueueStep label="Accepted" detail="Run saved" state="done" />
              <QueueStep label="In queue" detail="Waiting for capacity" state="active" />
              <QueueStep label="Worker starts" detail="Pipeline begins" state="pending" />
            </ol>

            <div className="mt-4 grid gap-2 sm:grid-cols-3 text-[12px]">
              <StatusFact
                icon={<Radio className="w-3.5 h-3.5" aria-hidden />}
                label={streamLabel}
                active={connected}
              />
              <StatusFact
                icon={<RefreshCw className="w-3.5 h-3.5" aria-hidden />}
                label={`Automatic backup checks every ${pollSeconds} seconds`}
              />
              <StatusFact
                icon={<Clock3 className="w-3.5 h-3.5" aria-hidden />}
                label={checkedLabel(lastCheckedAt, nowMs)}
              />
            </div>

            {view.safeToLeave && (
              <p
                className="mt-4 rounded-md px-3 py-2 text-[12px]"
                style={{ background: "var(--surface-muted)", color: "var(--ink-soft)" }}
              >
                Safe to leave this page — the run will start automatically and remain in Run history.
              </p>
            )}
          </div>
        </div>
      </div>

      <div
        className="border-t px-4 py-3 sm:px-5"
        style={{ borderColor: "var(--border-subtle)", background: "var(--surface-muted)" }}
      >
        <div className="label-eyebrow mb-2">Recent lifecycle updates</div>
        <ul className="space-y-2">
          {view.recentEvents.map((event, index) => (
            <li key={`${event.label}-${event.timestampMs ?? index}`} className="flex items-start gap-2.5 text-[12px]">
              <span className="dot dot-running mt-1.5 shrink-0 animate-pulse" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="font-medium" style={{ color: "var(--ink-strong)" }}>
                  {event.label}
                </span>
                <span style={{ color: "var(--ink-muted)" }}> — {event.detail}</span>
              </span>
              {event.timestampMs !== null && (
                <time
                  className="font-mono shrink-0"
                  style={{ color: "var(--ink-muted)" }}
                  dateTime={new Date(event.timestampMs).toISOString()}
                >
                  {eventTime(event.timestampMs)}
                </time>
              )}
            </li>
          ))}
        </ul>
        {acceptedAt !== null && (
          <span className="sr-only">Run accepted at {new Date(acceptedAt).toISOString()}.</span>
        )}
      </div>
    </section>
  );
}

function QueueStep({
  label,
  detail,
  state,
}: {
  label: string;
  detail: string;
  state: "done" | "active" | "pending";
}) {
  const color = state === "done" ? "var(--ok-ink)" : state === "active" ? "var(--accent-soft-ink)" : "var(--ink-muted)";
  const background = state === "done" ? "var(--ok-soft-bg)" : state === "active" ? "var(--accent-soft-bg-2)" : "var(--surface-muted)";
  return (
    <li className="rounded-md border p-2.5" style={{ borderColor: "var(--border-subtle)", background }}>
      <span className="flex items-center gap-1.5 text-[12px] font-medium" style={{ color }}>
        {state === "done" ? (
          <Check className="w-3.5 h-3.5" aria-hidden />
        ) : state === "active" ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />
        ) : (
          <CircleDashed className="w-3.5 h-3.5" aria-hidden />
        )}
        {label}
      </span>
      <span className="block mt-0.5 text-[11px]" style={{ color: "var(--ink-muted)" }}>
        {detail}
      </span>
    </li>
  );
}

function StatusFact({ icon, label, active = false }: { icon: React.ReactNode; label: string; active?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5" style={{ color: active ? "var(--ok-ink)" : "var(--ink-muted)" }}>
      {icon}
      {label}
    </span>
  );
}
