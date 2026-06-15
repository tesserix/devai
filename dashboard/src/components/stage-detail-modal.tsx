"use client";

import { useEffect, useMemo } from "react";
import { X, Users } from "lucide-react";
import { BoardroomGraph, type BoardroomMessage } from "@/components/boardroom-graph";
import { BoardroomFlow } from "@/components/boardroom-flow";

/**
 * Stage drill-in — click an agentic stage in the DAG to open this. Shows the
 * stage's multi-agent activity GRAPHICALLY (who's talking to whom / the debate
 * mesh, plus the round-by-round flow for the boardroom) AND the full
 * conversation as inspectable events: every request, response, and argument
 * with from→to, type, and timestamp, so you can validate exactly what each
 * agent said. Works for any agent stage; richest for the Boardroom Debate.
 */

export interface StageMessage {
  id?: string;
  from_agent?: string;
  to_agent?: string;
  message_type?: string;
  subject?: string;
  body?: string;
  timestamp?: string | number;
  payload?: { stage?: string } | null;
}

export interface StageInfo {
  name: string;
  title?: string;
  agent?: string;
  state?: string;
}

const TYPE_COLORS: Record<string, { bg: string; ink: string }> = {
  request: { bg: "var(--accent-soft-bg-2)", ink: "var(--accent-soft-ink)" },
  response: { bg: "var(--ok-soft-bg)", ink: "var(--ok-ink)" },
  broadcast: { bg: "var(--warn-soft-bg)", ink: "var(--warn-ink)" },
  handoff: { bg: "var(--accent-soft-bg-2)", ink: "var(--accent-soft-ink)" },
  escalation: { bg: "var(--error-soft-bg, var(--warn-soft-bg))", ink: "var(--error-ink, var(--warn-ink))" },
  notification: { bg: "var(--surface-muted)", ink: "var(--ink-muted)" },
};

function display(agent: string): string {
  return (agent || "")
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function fmtTime(t: string | number | undefined): string {
  if (t === undefined || t === null || t === "") return "";
  const ms = typeof t === "number" ? (t < 1e12 ? t * 1000 : t) : Date.parse(t);
  if (Number.isNaN(ms)) return String(t);
  return new Date(ms).toLocaleTimeString();
}

export function StageDetailModal({
  stage,
  messages,
  live = false,
  onClose,
}: {
  stage: StageInfo;
  messages: StageMessage[];
  live?: boolean;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isBoardroom = stage.name.toLowerCase().includes("boardroom") || stage.agent === "supervisor";

  const relevant = useMemo(() => {
    const rel = messages.filter((m) => {
      const ps = m.payload?.stage;
      if (ps) return ps === stage.name;
      if (isBoardroom) return m.to_agent === "boardroom" || m.from_agent === "supervisor";
      return m.from_agent === stage.agent || m.to_agent === stage.agent;
    });
    // Chronological for the transcript (oldest → newest reads like a debate).
    return rel.slice().sort((a, b) => {
      const ta = typeof a.timestamp === "number" ? a.timestamp : Date.parse(String(a.timestamp ?? 0)) / 1000;
      const tb = typeof b.timestamp === "number" ? b.timestamp : Date.parse(String(b.timestamp ?? 0)) / 1000;
      return (ta || 0) - (tb || 0);
    });
  }, [messages, stage.name, stage.agent, isBoardroom]);

  const graphMsgs: BoardroomMessage[] = relevant.map((m) => ({
    from_agent: m.from_agent,
    subject: m.subject,
    body: m.body,
    timestamp: typeof m.timestamp === "number" ? m.timestamp : Date.parse(String(m.timestamp ?? 0)) / 1000 || 0,
  }));
  const participants = new Set(relevant.flatMap((m) => [m.from_agent, m.to_agent].filter(Boolean) as string[]));

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.55)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl max-h-[88vh] overflow-y-auto rounded-xl border shadow-2xl"
        style={{ borderColor: "var(--accent-soft-bd)", background: "var(--surface)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div
          className="sticky top-0 flex items-center gap-2 px-4 py-3 border-b"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <Users className="w-4 h-4" style={{ color: "var(--accent)" }} />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold truncate" style={{ color: "var(--ink-strong)" }}>
              {stage.title || display(stage.name)}
            </h3>
            <p className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
              {stage.agent ? `${display(stage.agent)} · ` : ""}
              {participants.size} agent{participants.size === 1 ? "" : "s"} · {relevant.length} message
              {relevant.length === 1 ? "" : "s"}
              {live ? " · live" : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto p-1 rounded hover:opacity-80"
            style={{ color: "var(--ink-muted)" }}
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {relevant.length === 0 ? (
            <p className="text-[13px]" style={{ color: "var(--ink-muted)" }}>
              No agent conversation has been recorded for this stage yet. Once the agents start
              exchanging requests, responses, and arguments, they appear here — graphically and as a
              validatable transcript.
            </p>
          ) : (
            <>
              {/* Boardroom: the round-by-round workflow. */}
              {isBoardroom && <BoardroomFlow messages={graphMsgs} live={live} />}

              {/* The agent mesh — who is talking to / challenging whom. */}
              {participants.size > 1 && (
                <div>
                  <h4 className="text-[11px] font-semibold mb-1.5" style={{ color: "var(--ink-soft)" }}>
                    Agent interaction
                  </h4>
                  <BoardroomGraph messages={graphMsgs} live={live} />
                </div>
              )}

              {/* Full transcript — every request / response / argument. */}
              <div>
                <h4 className="text-[11px] font-semibold mb-1.5" style={{ color: "var(--ink-soft)" }}>
                  Conversation &amp; arguments ({relevant.length})
                </h4>
                <div className="space-y-2">
                  {relevant.map((m, i) => {
                    const t = (m.message_type || "message").toLowerCase();
                    const c = TYPE_COLORS[t] || TYPE_COLORS.notification;
                    return (
                      <div
                        key={m.id || i}
                        className="rounded-md border p-2.5"
                        style={{ borderColor: "var(--border)", background: "var(--surface-muted)" }}
                      >
                        <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                          <span className="text-[11px] font-semibold" style={{ color: "var(--ink-strong)" }}>
                            {display(m.from_agent || "?")}
                          </span>
                          <span className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
                            →
                          </span>
                          <span className="text-[11px] font-medium" style={{ color: "var(--ink)" }}>
                            {display(m.to_agent || "?")}
                          </span>
                          <span
                            className="px-1 py-0.5 rounded text-[9px] font-semibold uppercase"
                            style={{ background: c.bg, color: c.ink }}
                          >
                            {t}
                          </span>
                          <span className="ml-auto text-[10px] font-mono" style={{ color: "var(--ink-muted)" }}>
                            {fmtTime(m.timestamp)}
                          </span>
                        </div>
                        {m.subject && (
                          <p className="text-[12px] font-medium mb-0.5" style={{ color: "var(--ink-strong)" }}>
                            {m.subject}
                          </p>
                        )}
                        {m.body && (
                          <p className="text-[12px] whitespace-pre-wrap leading-relaxed" style={{ color: "var(--ink)" }}>
                            {m.body}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
