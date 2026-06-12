"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { StreamEvent } from "@/lib/api";

/**
 * Live terminal / event stream — stage transitions PLUS turn-level agent
 * activity (one block per LLM turn: token usage, the agent's narration,
 * and every tool call), in the style of Fiber's pretty agent log. Filter
 * chips narrow the stream; the header shows events · turns totals.
 */

// The terminal has a fixed dark background in every theme, so its text uses
// fixed light colors (NOT the theme --ink-* tokens, which would render
// dark-on-dark in light mode — the original bug used non-existent --text-*
// vars and rendered invisible).
const TERM_DIM = "#8a8f98";
const TERM_TEXT = "#e6e8eb";
const TERM_BLUE = "#60a5fa";
const TERM_GREEN = "#34d399";
const TERM_RED = "#f87171";
const TERM_AMBER = "#fbbf24";
const TERM_PURPLE = "#c4b5fd";

type Filter = "all" | "messages" | "tools" | "errors";

function phaseColor(ev: StreamEvent): string {
  if (ev.error) return TERM_RED;
  if (ev.phase === "completed") return TERM_GREEN;
  if (ev.phase === "failed") return TERM_RED;
  if (ev.phase === "skipped") return TERM_DIM;
  if (ev.phase === "started" || ev.phase === "running") return TERM_BLUE;
  return TERM_TEXT;
}

function fmtTime(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour12: false });
}

function matchesFilter(ev: StreamEvent, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "errors") return !!ev.error || ev.phase === "failed" || ev.kind === "tool_result";
  if (ev.event_type !== "agent_turn") return false;
  if (filter === "tools") return (ev.tools?.length ?? 0) > 0 || !!ev.tool;
  if (filter === "messages") return !!ev.text;
  return true;
}

function TurnRow({ ev }: { ev: StreamEvent }) {
  const t = fmtTime(ev.timestamp);
  if (ev.kind === "agent_start") {
    return (
      <div className="whitespace-pre-wrap break-words">
        <span style={{ color: TERM_DIM }}>{t} </span>
        <span style={{ color: TERM_PURPLE }}>
          ▶ agent_start {ev.agent || ""} · {ev.model || ""} · max {ev.max_turns ?? "?"} turns
        </span>
      </div>
    );
  }
  if (ev.kind === "agent_done") {
    return (
      <div className="whitespace-pre-wrap break-words">
        <span style={{ color: TERM_DIM }}>{t} </span>
        <span
          style={{
            color:
              ev.reason === "natural" || ev.reason === "remaining_none" ? TERM_GREEN : TERM_AMBER,
          }}
        >
          ✓ agent_done reason={ev.reason || "?"} · turns_used={ev.turns_used ?? "?"}
        </span>
      </div>
    );
  }
  if (ev.kind === "checkpoint") {
    return (
      <div
        className="whitespace-pre-wrap break-words pl-3 border-l"
        style={{ borderColor: "rgba(255,255,255,0.15)" }}
      >
        <span style={{ color: TERM_DIM }}>{t} </span>
        <span style={{ color: TERM_AMBER }}>⏸ session {ev.session ?? "?"} checkpoint</span>
        {ev.text && <div style={{ color: TERM_TEXT }}>{ev.text}</div>}
      </div>
    );
  }
  if (ev.kind === "tool_result") {
    return (
      <div className="whitespace-pre-wrap break-words">
        <span style={{ color: TERM_DIM }}>{t} </span>
        <span style={{ color: TERM_RED }}>
          ✗ {ev.tool} {ev.error}
        </span>
      </div>
    );
  }
  // kind === "turn"
  return (
    <div className="mt-1.5">
      <div style={{ color: TERM_DIM }}>
        {"TURN "}
        {ev.turn ?? "?"}
        {typeof ev.session === "number" ? ` · session ${ev.session}` : ""} {"─".repeat(8)}
      </div>
      <div>
        <span style={{ color: TERM_DIM }}>
          {t} usage · in {ev.usage_in ?? 0} · out {ev.usage_out ?? 0}
          {ev.cache_read ? ` · cached ${ev.cache_read}` : ""}
        </span>
      </div>
      {ev.text && (
        <div
          className="pl-3 border-l whitespace-pre-wrap break-words"
          style={{ borderColor: "rgba(255,255,255,0.15)", color: TERM_TEXT }}
        >
          {ev.text}
        </div>
      )}
      {(ev.tools ?? []).map((tc, i) => (
        <div key={i} className="whitespace-pre-wrap break-words">
          <span style={{ color: TERM_DIM }}>{t} </span>
          <span style={{ color: TERM_BLUE }}>⚙ {tc.name}</span>{" "}
          <span style={{ color: TERM_TEXT }}>{tc.input || ""}</span>
        </div>
      ))}
    </div>
  );
}

export function TerminalPanel({ events }: { events: StreamEvent[] }) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const stats = useMemo(() => {
    const turns = events.filter((e) => e.event_type === "agent_turn" && e.kind === "turn").length;
    return { events: events.length, turns };
  }, [events]);

  const visible = useMemo(() => events.filter((e) => matchesFilter(e, filter)), [events, filter]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [visible.length]);

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
        <div className="ml-3 flex items-center gap-1">
          {(["all", "messages", "tools", "errors"] as Filter[]).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className="rounded-full px-2 py-0.5 text-[10px] font-mono"
              style={{
                color: filter === f ? "#0d1117" : TERM_DIM,
                background: filter === f ? TERM_BLUE : "rgba(255,255,255,0.06)",
              }}
            >
              {f}
            </button>
          ))}
        </div>
        <span className="ml-auto text-[10px] font-mono" style={{ color: TERM_DIM }}>
          {stats.events} events{stats.turns > 0 ? ` · ${stats.turns} turns` : ""}
        </span>
      </div>

      <div className="flex-1 overflow-auto p-3 font-mono text-[12px] leading-relaxed">
        {visible.length === 0 ? (
          <span className="inline-flex items-center gap-2" style={{ color: TERM_DIM }}>
            <span
              className="inline-block h-2 w-2 animate-pulse rounded-full"
              style={{ background: TERM_BLUE }}
            />
            {events.length === 0 ? "Waiting for the agent to start…" : "Nothing matches this filter."}
          </span>
        ) : (
          visible.map((ev, i) =>
            ev.event_type === "agent_turn" ? (
              <TurnRow key={i} ev={ev} />
            ) : (
              <div key={i} className="whitespace-pre-wrap break-words">
                <span style={{ color: TERM_DIM }}>
                  {fmtTime(ev.timestamp)} {ev.stage ? `${ev.stage}` : "•"}
                  {ev.phase ? ` · ${ev.phase}` : ""}
                </span>{" "}
                <span style={{ color: phaseColor(ev) }}>{ev.error || ev.message || ""}</span>
              </div>
            ),
          )
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
