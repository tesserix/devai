export interface QueuedLifecycleEvent {
  stage?: string;
  phase?: string;
  message?: string;
  error?: string;
  step?: string;
  status?: string;
  detail?: string;
  timestamp?: string | number;
}

export interface QueuedRunSnapshot {
  created_at?: string | number;
  updated_at?: string | number;
  started_at?: string | number | null;
  stage_events?: QueuedLifecycleEvent[];
  events?: QueuedLifecycleEvent[];
}

export interface QueuedRunEventView {
  label: string;
  detail: string;
  timestampMs: number | null;
}

export interface QueuedRunView {
  elapsedLabel: string;
  safeToLeave: boolean;
  updatedAtMs: number | null;
  recentEvents: QueuedRunEventView[];
}

export function runTimestampMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value >= 1_000_000_000_000 ? value : value * 1_000;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return runTimestampMs(numeric);
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatQueueElapsed(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds === 0) return "just now";
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    const minuteText = `${minutes} minute${minutes === 1 ? "" : "s"}`;
    if (remainingSeconds === 0) return minuteText;
    return `${minuteText} ${remainingSeconds} second${remainingSeconds === 1 ? "" : "s"}`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  const hourText = `${hours} hour${hours === 1 ? "" : "s"}`;
  if (remainingMinutes === 0) return hourText;
  return `${hourText} ${remainingMinutes} minute${remainingMinutes === 1 ? "" : "s"}`;
}

function eventView(event: QueuedLifecycleEvent): QueuedRunEventView | null {
  const stage = (event.stage || event.step || "").trim();
  const phase = (event.phase || event.status || "").trim();
  if (!stage && !phase) return null;
  const label = [stage.replace(/[_-]/g, " "), phase.replace(/_/g, " ")]
    .filter(Boolean)
    .join(" · ");
  return {
    label,
    detail: (event.message || event.error || event.detail || "Lifecycle update received.").trim(),
    timestampMs: runTimestampMs(event.timestamp),
  };
}

export function buildQueuedRunView(run: QueuedRunSnapshot, nowMs: number): QueuedRunView {
  const createdAtMs = runTimestampMs(run.created_at);
  const elapsedSeconds = createdAtMs === null ? 0 : Math.max(0, (nowMs - createdAtMs) / 1_000);
  const sourceEvents = run.stage_events?.length ? run.stage_events : (run.events ?? []);
  const recentEvents = sourceEvents
    .map(eventView)
    .filter((event): event is QueuedRunEventView => event !== null)
    .slice(-3)
    .reverse();

  if (recentEvents.length === 0) {
    recentEvents.push({
      label: "Run accepted",
      detail: "DevAI saved the run and is waiting for an available worker.",
      timestampMs: createdAtMs,
    });
  }

  return {
    elapsedLabel: formatQueueElapsed(elapsedSeconds),
    safeToLeave: elapsedSeconds >= 60,
    updatedAtMs: runTimestampMs(run.updated_at) ?? createdAtMs,
    recentEvents,
  };
}
