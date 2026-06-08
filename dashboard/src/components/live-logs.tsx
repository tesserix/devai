"use client";

import { useEffect, useRef, useState } from "react";
import { Wifi, WifiOff } from "lucide-react";
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

function dedupeAppend(prev: StreamEvent[], next: StreamEvent): StreamEvent[] {
  const key = (e: StreamEvent) =>
    `${e.timestamp}|${e.stage ?? ""}|${e.phase ?? ""}|${e.message ?? ""}`;
  const k = key(next);
  const tail = prev.slice(-SSE_REPLAY);
  if (tail.some((e) => key(e) === k)) return prev;
  return [...prev, next];
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
