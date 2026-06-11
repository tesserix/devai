"use client";

/**
 * useRunEvents — one EventSource per page driving every live tab.
 *
 * Subscribes to /api/pipeline/events/stream (typed envelopes: stage |
 * agent_status | a2a) filtered to one run, and invokes `onEvent` for each
 * matching envelope. The canonical consumption pattern is SSE-TRIGGERED
 * REFRESH: the backend hub mutates the task (agents dict, a2a, routing)
 * BEFORE emitting the envelope, so a debounced re-fetch of the run snapshot
 * right after any envelope yields a consistent, complete view — no client-
 * side state merging, no drift. Polling stays as reconciliation: callers
 * slow their poll while `connected` is true.
 *
 * Reconnects automatically with exponential backoff and resumes from
 * Last-Event-ID (the server replays from its ring buffer).
 */

import { useEffect, useRef, useState } from "react";

export interface RunEventEnvelope {
  timestamp: number;
  task_id: string;
  event_type?: "stage" | "agent_status" | "a2a";
  [key: string]: unknown;
}

const EVENT_TYPES = ["stage", "agent_status", "a2a"] as const;
const REPLAY = 100;
const MAX_BACKOFF_MS = 15_000;

export function useRunEvents(
  taskId: string | null | undefined,
  onEvent: (envelope: RunEventEnvelope) => void,
): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  // Keep the handler in a ref so handler identity never forces a reconnect.
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    if (!taskId) {
      setConnected(false);
      return;
    }
    let es: EventSource | null = null;
    let closedByUs = false;
    let backoff = 1_000;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    const lastEventId = { current: "" };

    const connect = () => {
      const params = new URLSearchParams({ replay: String(REPLAY) });
      if (lastEventId.current) params.set("last_event_id", lastEventId.current);
      es = new EventSource(`/api/pipeline/events/stream?${params.toString()}`, {
        withCredentials: true,
      });
      es.onopen = () => {
        backoff = 1_000;
        setConnected(true);
      };
      for (const type of EVENT_TYPES) {
        es.addEventListener(type, (e) => {
          const me = e as MessageEvent;
          if (me.lastEventId) lastEventId.current = me.lastEventId;
          try {
            const data = JSON.parse(me.data) as RunEventEnvelope;
            if (data.task_id !== taskId) return;
            handlerRef.current({ ...data, event_type: data.event_type ?? type });
          } catch {
            /* ignore malformed frames */
          }
        });
      }
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (closedByUs) return;
        retryTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      };
    };

    connect();
    return () => {
      closedByUs = true;
      if (retryTimer) clearTimeout(retryTimer);
      es?.close();
      setConnected(false);
    };
  }, [taskId]);

  return { connected };
}
