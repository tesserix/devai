"use client";

import { useMemo, type ReactNode } from "react";
import type { BoardroomMessage } from "@/components/boardroom-graph";

/**
 * Boardroom debate FLOW — the whole workflow as a left-to-right stepper.
 *
 * Where BoardroomGraph shows the debate as a network (who challenged whom),
 * this shows it as a PROCESS: Convened → Round 1 → (recruited?) → Round 2 →
 * … → Decision. Each round lists the seats that spoke and the supervisor's
 * synthesis. Built purely from the A2A messages the stage emits (subjects
 * like "Round 2 position" / "Round 2 synthesis" / "Recruited …" / "Boardroom
 * decision"), so it streams live as the debate happens and persists after.
 */

const PALETTE = ["#10b981", "#f59e0b", "#8b5cf6", "#0ea5e9", "#ec4899", "#14b8a6", "#f97316"];

function display(agent: string): string {
  return agent
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function seatColor(agent: string): string {
  const h = [...agent].reduce((a, c) => a + c.charCodeAt(0), 0);
  return PALETTE[h % PALETTE.length];
}

interface Round {
  n: number;
  speakers: string[]; // agent keys, in speaking order
  synthesis: string;
  recruited: string[];
}

export function BoardroomFlow({
  messages,
  consensus,
  decided = false,
  live = false,
}: {
  messages: BoardroomMessage[];
  consensus?: boolean;
  decided?: boolean;
  live?: boolean;
}) {
  const flow = useMemo(() => {
    let convened = false;
    let panel: string[] = [];
    const rounds = new Map<number, Round>();
    let decision = false;

    const round = (n: number): Round => {
      let r = rounds.get(n);
      if (!r) {
        r = { n, speakers: [], synthesis: "", recruited: [] };
        rounds.set(n, r);
      }
      return r;
    };

    for (const m of messages) {
      const subj = (m.subject || "").toLowerCase();
      const from = m.from_agent || "";
      if (subj.includes("convened")) {
        convened = true;
        // "Panel: A, B, C. Topic: …" — pull the named seats for the roster.
        const body = m.body || "";
        const match = body.match(/panel:\s*([^.]+)/i);
        if (match) panel = match[1].split(",").map((s) => s.trim()).filter(Boolean);
        continue;
      }
      if (subj.includes("decision")) {
        decision = true;
        continue;
      }
      const rn = subj.match(/round\s+(\d+)/);
      const n = rn ? parseInt(rn[1], 10) : 0;
      if (subj.startsWith("recruited")) {
        // "Recruited <Name>" — attach to the most recent round in play.
        const name = (m.subject || "").replace(/^recruited\s+/i, "").trim();
        if (name) round(n || 1).recruited.push(name);
        continue;
      }
      if (n > 0 && from === "supervisor" && subj.includes("synthesis")) {
        round(n).synthesis = m.body || "";
        continue;
      }
      if (n > 0 && from && from !== "supervisor" && subj.includes("position")) {
        const r = round(n);
        if (!r.speakers.includes(from)) r.speakers.push(from);
      }
    }
    return { convened, panel, rounds: [...rounds.values()].sort((a, b) => a.n - b.n), decision };
  }, [messages]);

  if (!flow.convened && flow.rounds.length === 0) return null;

  const Arrow = () => (
    <div className="flex items-center px-1 shrink-0" style={{ color: "var(--ink-muted)" }} aria-hidden>
      →
    </div>
  );

  const Step = ({
    title,
    accent,
    pulse,
    children,
  }: {
    title: string;
    accent: string;
    pulse?: boolean;
    children?: ReactNode;
  }) => (
    <div
      className="shrink-0 rounded-md border p-2 min-w-[132px] max-w-[190px]"
      style={{
        borderColor: pulse ? accent : "var(--border)",
        background: "var(--surface)",
        boxShadow: pulse ? `0 0 0 1px ${accent}` : undefined,
      }}
    >
      <div className="flex items-center gap-1.5 mb-1">
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: accent }} />
        <span className="text-[11px] font-semibold" style={{ color: "var(--ink-strong)" }}>
          {title}
        </span>
        {pulse && (
          <span className="ml-auto text-[9px] font-medium" style={{ color: accent }}>
            live
          </span>
        )}
      </div>
      {children}
    </div>
  );

  const lastRoundN = flow.rounds.length ? flow.rounds[flow.rounds.length - 1].n : 0;

  return (
    <div className="mb-3">
      <div className="flex items-stretch gap-0 overflow-x-auto pb-1">
        {/* Convened */}
        <Step title="Convened" accent="#8b5cf6">
          <div className="flex flex-wrap gap-1">
            {flow.panel.slice(0, 6).map((p) => (
              <span
                key={p}
                className="px-1 py-0.5 rounded text-[9px]"
                style={{ background: "var(--surface-muted)", color: "var(--ink)" }}
              >
                {p}
              </span>
            ))}
          </div>
        </Step>

        {flow.rounds.map((r) => {
          const isLast = r.n === lastRoundN;
          const pulse = live && isLast && !flow.decision;
          return (
            <div key={r.n} className="flex items-stretch">
              <Arrow />
              <Step title={`Round ${r.n}`} accent="#0ea5e9" pulse={pulse}>
                <div className="flex flex-wrap gap-1 mb-1">
                  {r.speakers.map((s) => (
                    <span
                      key={s}
                      title={display(s)}
                      className="px-1 py-0.5 rounded text-[9px] font-medium"
                      style={{ background: `${seatColor(s)}22`, color: seatColor(s) }}
                    >
                      {display(s)}
                    </span>
                  ))}
                </div>
                {r.recruited.length > 0 && (
                  <p className="text-[9px] mb-0.5" style={{ color: "#f59e0b" }}>
                    + recruited {r.recruited.join(", ")}
                  </p>
                )}
                {r.synthesis && (
                  <p className="text-[9.5px] leading-snug line-clamp-3" style={{ color: "var(--ink-muted)" }}>
                    {r.synthesis.replace(/\s+/g, " ").slice(0, 120)}
                  </p>
                )}
              </Step>
            </div>
          );
        })}

        {/* Decision */}
        {(flow.decision || decided) && (
          <div className="flex items-stretch">
            <Arrow />
            <Step title="Decision" accent={consensus ? "#10b981" : "#f59e0b"}>
              <span
                className="px-1.5 py-0.5 rounded-full text-[9px] font-semibold"
                style={{
                  background: consensus ? "var(--ok-soft-bg)" : "var(--warn-soft-bg)",
                  color: consensus ? "var(--ok-ink)" : "var(--warn-ink)",
                }}
              >
                {consensus ? "consensus" : "majority + dissent"}
              </span>
            </Step>
          </div>
        )}
      </div>
    </div>
  );
}
