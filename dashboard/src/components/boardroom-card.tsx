"use client";

import { useState } from "react";
import { Users } from "lucide-react";
import { BoardroomGraph, type BoardroomMessage } from "@/components/boardroom-graph";

/**
 * Boardroom outcome card — the dedicated view for the "Brainstorm first"
 * debate. Shows who sat at the table, whether the panel reached consensus,
 * the agreed decision document, and (expandable) the full debate transcript.
 * Rendered on the run Overview whenever the boardroom stage produced a
 * decision; the live debate streams in the Timeline tab as it happens.
 */

export interface BoardroomOutcome {
  decision?: string;
  panel?: string[];
  consensus?: boolean;
  budget_hit?: boolean;
  transcript?: string;
}

export function BoardroomCard({
  outcome,
  messages = [],
  live = false,
}: {
  outcome: BoardroomOutcome | null;
  messages?: BoardroomMessage[];
  live?: boolean;
}) {
  const [showTranscript, setShowTranscript] = useState(false);
  // Show during a LIVE debate (graph only) and after (graph + decision).
  if (!outcome?.decision && messages.length === 0) return null;

  return (
    <section
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--accent-soft-bd)", background: "var(--surface)" }}
    >
      <div className="flex items-center gap-2 mb-2">
        <Users className="w-4 h-4" style={{ color: "var(--accent)" }} />
        <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
          Boardroom decision
        </h3>
        {outcome?.decision && (
          <span
            className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold"
            style={{
              background: outcome.consensus ? "var(--ok-soft-bg)" : "var(--warn-soft-bg)",
              color: outcome.consensus ? "var(--ok-ink)" : "var(--warn-ink)",
            }}
          >
            {outcome.consensus ? "consensus" : "majority + dissent"}
          </span>
        )}
        {outcome?.budget_hit && (
          <span
            className="px-1.5 py-0.5 rounded-full text-[10px]"
            style={{ background: "var(--warn-soft-bg)", color: "var(--warn-ink)" }}
            title="The debate hit its time budget — this is the best position on the table."
          >
            time budget reached
          </span>
        )}
      </div>

      {messages.length > 0 && (
        <div className="mb-3">
          <BoardroomGraph messages={messages} live={live} />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 mb-3">
        {(outcome?.panel ?? []).map((p) => (
          <span
            key={p}
            className="px-1.5 py-0.5 rounded-full text-[10px]"
            style={{ background: "var(--accent-soft-bg-2)", color: "var(--accent-soft-ink)" }}
          >
            {p}
          </span>
        ))}
      </div>

      {outcome?.decision ? (
        <div
          className="text-[13px] whitespace-pre-wrap leading-relaxed rounded p-3"
          style={{ background: "var(--surface-muted)", color: "var(--ink)", maxHeight: 320, overflowY: "auto" }}
        >
          {outcome.decision}
        </div>
      ) : (
        <p className="text-xs italic" style={{ color: "var(--ink-muted)" }}>
          The panel is debating live — the agreed decision lands here for your approval.
        </p>
      )}

      {outcome?.transcript && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowTranscript((v) => !v)}
            className="text-xs font-medium hover:underline"
            style={{ color: "var(--accent)" }}
          >
            {showTranscript ? "Hide debate transcript" : "Show debate transcript"}
          </button>
          {showTranscript && (
            <pre
              className="mt-2 text-[11px] whitespace-pre-wrap rounded p-3 font-mono"
              style={{ background: "var(--surface-muted)", color: "var(--ink-soft)", maxHeight: 400, overflowY: "auto" }}
            >
              {outcome?.transcript}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
