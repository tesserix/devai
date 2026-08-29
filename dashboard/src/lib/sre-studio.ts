export type StudioDraftSummary = {
  name: string;
  kind: string;
  status: string;
};

export type StudioStageEvent = {
  phase: string;
  duration_ms?: number | null;
};

export function filterStudioDrafts<T extends StudioDraftSummary>(drafts: T[], query: string): T[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) return drafts;

  return drafts.filter((draft) =>
    [draft.name, draft.kind, draft.status].some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    ),
  );
}

export function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs == null) return "—";

  const safeDurationMs = Math.max(0, durationMs);
  if (safeDurationMs < 1_000) return `${Math.round(safeDurationMs)}ms`;
  if (safeDurationMs < 60_000) return `${(safeDurationMs / 1_000).toFixed(1)}s`;

  const totalSeconds = Math.round(safeDurationMs / 1_000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds === 0 ? `${minutes}m` : `${minutes}m ${seconds}s`;
}

export function summarizeStageEvents<T extends StudioStageEvent>(events: T[]) {
  const terminalEvents = events.filter((event) => event.phase !== "started");
  const timedEvents = terminalEvents.filter((event) => event.duration_ms != null);

  return {
    events: terminalEvents,
    completed: terminalEvents.filter((event) => event.phase === "completed").length,
    failed: terminalEvents.filter((event) => event.phase === "failed").length,
    skipped: terminalEvents.filter((event) => event.phase === "skipped").length,
    totalDurationMs:
      timedEvents.length === 0
        ? null
        : timedEvents.reduce((total, event) => total + (event.duration_ms ?? 0), 0),
  };
}

export async function persistStudioDraft<T extends { id: string; yaml: string }>(
  draft: T,
  yaml: string,
  update: (id: string, input: { yaml: string }) => Promise<T>,
): Promise<T> {
  if (draft.yaml === yaml) return draft;
  return update(draft.id, { yaml });
}
