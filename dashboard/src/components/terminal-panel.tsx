"use client";

import { useEffect, useRef } from "react";
import type { StreamEvent } from "@/lib/api";

/**
 * Live terminal / event stream — the "Terminal" panel from the Cursor
 * screenshot. Renders the stage + terminal (LOG::) lines the runner streams
 * over /api/pipeline/events/stream for the active task, newest at the bottom.
 */

// The terminal has a fixed dark background in every theme, so its text uses
// fixed light colors (NOT the theme --ink-* tokens, which would render
// dark-on-dark in light mode — the original bug used non-existent --text-*
// vars and rendered invisible).
const TERM_DIM = "#8a8f98";
const TERM_TEXT = "#e6e8eb";

function phaseColor(ev: StreamEvent): string {
  if (ev.error) return "#f87171";
  if (ev.phase === "completed") return "#34d399";
  if (ev.phase === "failed") return "#f87171";
  if (ev.phase === "skipped") return TERM_DIM;
  if (ev.phase === "started" || ev.phase === "running") return "#60a5fa";
  return TERM_TEXT;
}

export function TerminalPanel({ events }: { events: StreamEvent[] }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);

  return (
    <div
      className="flex h-full flex-col overflow-hidden rounded-lg border"
      style={{ background: "#0d1117", borderColor: "var(--border-subtle)" }}
    >
      <div
        className="flex items-center gap-2 border-b px-3 py-2"
        style={{ borderColor: "rgba(255,255,255,0.08)" }}
      >
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#ef4444" }} />
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#eab308" }} />
        <span className="h-2.5 w-2.5 rounded-full" style={{ background: "#22c55e" }} />
        <span className="ml-2 text-xs" style={{ color: TERM_DIM }}>
          Terminal
        </span>
      </div>

      <div className="flex-1 overflow-auto p-3 font-mono text-[12px] leading-relaxed">
        {events.length === 0 ? (
          <span className="inline-flex items-center gap-2" style={{ color: TERM_DIM }}>
            <span className="inline-block h-2 w-2 animate-pulse rounded-full" style={{ background: "#60a5fa" }} />
            Waiting for the agent to start…
          </span>
        ) : (
          events.map((ev, i) => (
            <div key={i} className="whitespace-pre-wrap break-words">
              <span style={{ color: TERM_DIM }}>
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
