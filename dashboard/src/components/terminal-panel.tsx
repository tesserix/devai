"use client";

import { useEffect, useRef } from "react";
import type { StreamEvent } from "@/lib/api";

/**
 * Live terminal / event stream — the "Terminal" panel from the Cursor
 * screenshot. Renders the stage + terminal (LOG::) lines the runner streams
 * over /api/pipeline/events/stream for the active task, newest at the bottom.
 */

function phaseColor(ev: StreamEvent): string {
  if (ev.error) return "#ef4444";
  if (ev.phase === "completed") return "#22c55e";
  if (ev.phase === "failed") return "#ef4444";
  if (ev.phase === "skipped") return "var(--text-muted)";
  return "var(--text-strong)";
}

export function TerminalPanel({ events }: { events: StreamEvent[] }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);

  return (
    <div
      className="flex h-full flex-col overflow-hidden rounded-lg border"
      style={{ background: "var(--surface-strong, #0c0c0c)", borderColor: "var(--border-subtle)" }}
    >
      <div
        className="flex items-center gap-2 border-b px-3 py-2"
        style={{ borderColor: "var(--border-subtle)" }}
      >
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#ef4444" }} />
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#eab308" }} />
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#22c55e" }} />
        <span className="ml-2 text-xs" style={{ color: "var(--text-muted)" }}>
          Terminal
        </span>
      </div>

      <div className="flex-1 overflow-auto p-3 font-mono text-[12px] leading-relaxed">
        {events.length === 0 ? (
          <span style={{ color: "var(--text-muted)" }}>Waiting for the agent to start…</span>
        ) : (
          events.map((ev, i) => (
            <div key={i} className="whitespace-pre-wrap break-words">
              <span style={{ color: "var(--text-muted)" }}>
                {ev.stage ? `${ev.stage}` : "•"}
                {ev.phase ? ` · ${ev.phase}` : ""}
              </span>{" "}
              <span style={{ color: phaseColor(ev) }}>{ev.error || ev.message || ""}</span>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
