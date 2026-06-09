"use client";

import { useEffect, useRef, useState } from "react";
import { Wifi, WifiOff } from "lucide-react";
import { api } from "@/lib/api";
import type { StreamEvent } from "@/lib/api";
import { TerminalPanel } from "@/components/terminal-panel";

/**
 * LiveLogs — a self-contained live terminal for one run/task.
 *
 * Subscribes to the global pipeline SSE feed (/api/pipeline/events/stream),
 * filters to this task_id, and renders the per-stage log lines as they arrive
 * (newest at the bottom). On (re)connect it asks the server to replay recent
 * frames + anything after the last id we saw, so a dropped socket backfills
 * instead of leaving a hole. Used by the run-detail "Logs" tab; the Compose
 * page has its own inline copy of this stream wired to its checkpoint timeline.
 */

const SSE_REPLAY = 200;

type Conn = "connecting" | "open" | "closed";

const eventKey = (e: StreamEvent) =>
  `${e.stage ?? ""}|${e.phase ?? ""}|${e.message ?? ""}`;

function dedupeAppend(prev: StreamEvent[], next: StreamEvent): StreamEvent[] {
  const k = eventKey(next);
  const tail = prev.slice(-SSE_REPLAY);
  if (tail.some((e) => eventKey(e) === k)) return prev;
  return [...prev, next];
}

// Merge a batch (e.g. the run snapshot's stage_events) into the stream, deduped
// by stage|phase|message and sorted by timestamp. This is the RELIABLE source:
// the SSE feed is proxied through Next.js rewrites which buffer streaming
// responses, so the browser's EventSource can silently deliver nothing. Polling
// the run snapshot's stage_events over the plain GET path (which is NOT buffered)
// guarantees the terminal shows progress; the SSE layer just adds lower latency
// when it does come through.
function mergeBatch(prev: StreamEvent[], batch: StreamEvent[]): StreamEvent[] {
  const seen = new Set(prev.map(eventKey));
  const fresh = batch.filter((e) => !seen.has(eventKey(e)));
  if (fresh.length === 0) return prev;
  return [...prev, ...fresh].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));
}

export function LiveLogs({ taskId, className }: { taskId: string; className?: string }) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [conn, setConn] = useState<Conn>("connecting");
  const lastEventIdRef = useRef<string>("");

  useEffect(() => {
    if (!taskId) return;
    let es: EventSource | null = null;
    let closedByUs = false;

    const connect = () => {
      const params = new URLSearchParams({ replay: String(SSE_REPLAY), task_id: taskId });
      if (lastEventIdRef.current) params.set("last_event_id", lastEventIdRef.current);
      setConn("connecting");
      es = new EventSource(`/api/pipeline/events/stream?${params.toString()}`, {
        withCredentials: true,
      });
      es.onopen = () => setConn("open");
      es.addEventListener("stage", (e) => {
        const me = e as MessageEvent;
        if (me.lastEventId) lastEventIdRef.current = me.lastEventId;
        try {
          const data = JSON.parse(me.data) as StreamEvent;
          if (data.task_id !== taskId) return;
          setEvents((prev) => dedupeAppend(prev, data));
        } catch {
          /* ignore malformed frames */
        }
      });
      es.onerror = () => {
        if (!closedByUs) setConn("closed");
      };
    };

    connect();
    return () => {
      closedByUs = true;
      es?.close();
    };
  }, [taskId]);

  // Reliable fallback: poll the run snapshot's stage_events over the plain GET
  // path and merge them in. The SSE feed above is the nicer live layer, but it's
  // proxied through Next.js rewrites that buffer streaming responses, so on its
  // own the terminal can sit empty ("Waiting for the agent…") even while stages
  // run. This poll guarantees the per-stage history shows up regardless.
  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const run = (await api.getRun(taskId)) as {
          stage_events?: Array<{
            stage?: string;
            phase?: string;
            message?: string;
            error?: string | null;
            timestamp?: number;
            duration_ms?: number;
          }>;
        };
        const se = run?.stage_events ?? [];
        if (cancelled || se.length === 0) return;
        const batch: StreamEvent[] = se.map((e, i) => ({
          timestamp: e.timestamp ?? i,
          task_id: taskId,
          stage: e.stage,
          phase: e.phase,
          message: e.message,
          error: e.error ?? null,
        }));
        setEvents((prev) => mergeBatch(prev, batch));
      } catch {
        /* transient — keep last data */
      }
    };
    poll();
    const id = window.setInterval(poll, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [taskId]);

  return (
    <div className={`flex flex-col ${className ?? ""}`}>
      <div className="mb-2 flex items-center justify-between">
        <span className="label-eyebrow">Live activity · per-stage stream</span>
        <span
          className="inline-flex items-center gap-1.5 text-[11px]"
          style={{ color: "var(--ink-muted)" }}
        >
          {conn === "open" ? (
            <Wifi className="w-3.5 h-3.5" style={{ color: "var(--ok)" }} aria-hidden />
          ) : (
            <WifiOff className="w-3.5 h-3.5" aria-hidden />
          )}
          {conn === "open" ? "Live" : conn === "closed" ? "Reconnecting…" : "Connecting…"}
        </span>
      </div>
      <div className="h-[60vh] min-h-[360px]">
        <TerminalPanel events={events} />
      </div>
    </div>
  );
}
