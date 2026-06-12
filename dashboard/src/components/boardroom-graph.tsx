"use client";

import { useMemo } from "react";

/**
 * Boardroom sprawl graph — the debate as a living network, not a sequence.
 *
 * Layout: the Supervisor (moderator) sits at the center taking notes; every
 * panelist is a node around it. Edges:
 *   - dim spokes  seat ↔ center   (the supervisor tracks every statement)
 *   - accent arcs seat ↔ seat     (A challenged B — derived from name
 *                                  mentions in each statement's text)
 * Node size grows with participation; the most recent speaker pulses while
 * the debate is live. Pure SVG — no physics lib, deterministic layout with
 * a slight organic jitter so it reads like the reference force graphs.
 */

export interface BoardroomMessage {
  from_agent?: string;
  subject?: string;
  body?: string;
  timestamp?: number;
}

const PALETTE = ["#10b981", "#f59e0b", "#8b5cf6", "#0ea5e9", "#ec4899", "#14b8a6", "#f97316"];

function display(agent: string): string {
  return agent
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function BoardroomGraph({
  messages,
  live = false,
}: {
  messages: BoardroomMessage[];
  live?: boolean;
}) {
  const model = useMemo(() => {
    const seats = new Map<string, { count: number; last: string }>();
    for (const m of messages) {
      const from = m.from_agent || "";
      if (!from || from === "supervisor") continue;
      const entry = seats.get(from) ?? { count: 0, last: "" };
      entry.count += 1;
      entry.last = (m.body || m.subject || "").slice(0, 160);
      seats.set(from, entry);
    }
    const names = [...seats.keys()];
    // Challenge edges: A's statement mentions B's display name.
    const edges: Array<[string, string]> = [];
    for (const m of messages) {
      const from = m.from_agent || "";
      if (!from || from === "supervisor") continue;
      const text = (m.body || "").toLowerCase();
      for (const other of names) {
        if (other === from) continue;
        const label = display(other).toLowerCase();
        const firstWord = label.split(" ")[0];
        if (text.includes(label) || (firstWord.length > 4 && text.includes(firstWord))) {
          edges.push([from, other]);
        }
      }
    }
    const lastSpeaker = [...messages].reverse().find((m) => m.from_agent && m.from_agent !== "supervisor")?.from_agent;
    const noteCount = messages.filter((m) => m.from_agent === "supervisor").length;
    return { names, seats, edges, lastSpeaker, noteCount };
  }, [messages]);

  if (model.names.length === 0) return null;

  const W = 560;
  const H = 360;
  const cx = W / 2;
  const cy = H / 2;
  const R = Math.min(W, H) / 2 - 56;

  const pos = new Map<string, { x: number; y: number }>();
  model.names.forEach((name, i) => {
    const angle = (2 * Math.PI * i) / model.names.length - Math.PI / 2;
    // Deterministic organic jitter from the name hash.
    const h = [...name].reduce((a, c) => a + c.charCodeAt(0), 0);
    const jr = ((h % 23) - 11) * 1.6;
    pos.set(name, {
      x: cx + (R + jr) * Math.cos(angle),
      y: cy + (R + jr) * Math.sin(angle),
    });
  });

  const dedupedEdges = [...new Set(model.edges.map(([a, b]) => (a < b ? `${a}|${b}` : `${b}|${a}`)))];

  return (
    <div className="rounded-lg p-2" style={{ background: "var(--surface-muted)" }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Boardroom debate graph">
        {/* spokes: supervisor tracks every seat */}
        {model.names.map((name) => {
          const p = pos.get(name)!;
          return (
            <line
              key={`spoke-${name}`}
              x1={cx}
              y1={cy}
              x2={p.x}
              y2={p.y}
              stroke="var(--border)"
              strokeWidth={1}
              strokeDasharray="3 4"
              opacity={0.6}
            />
          );
        })}

        {/* challenge arcs: seat ↔ seat */}
        {dedupedEdges.map((key) => {
          const [a, b] = key.split("|");
          const pa = pos.get(a);
          const pb = pos.get(b);
          if (!pa || !pb) return null;
          const mx = (pa.x + pb.x) / 2 + (cy - (pa.y + pb.y) / 2) * 0.25;
          const my = (pa.y + pb.y) / 2 + ((pa.x + pb.x) / 2 - cx) * 0.25;
          return (
            <path
              key={key}
              d={`M ${pa.x} ${pa.y} Q ${mx} ${my} ${pb.x} ${pb.y}`}
              fill="none"
              stroke="#f59e0b"
              strokeWidth={1.5}
              opacity={0.75}
            >
              {live && (
                <animate attributeName="opacity" values="0.75;0.25;0.75" dur="2.2s" repeatCount="indefinite" />
              )}
            </path>
          );
        })}

        {/* supervisor (moderator) center node */}
        <g>
          {live && (
            <circle cx={cx} cy={cy} r={26} fill="none" stroke="#8b5cf6" strokeWidth={2}>
              <animate attributeName="r" values="22;30;22" dur="1.8s" repeatCount="indefinite" />
              <animate attributeName="opacity" values="0.8;0.1;0.8" dur="1.8s" repeatCount="indefinite" />
            </circle>
          )}
          <circle cx={cx} cy={cy} r={20} fill="#8b5cf6" opacity={0.92} />
          <text x={cx} y={cy + 3.5} textAnchor="middle" style={{ fontSize: 9, fontWeight: 700, fill: "#fff" }}>
            SUP
          </text>
          <text x={cx} y={cy + 34} textAnchor="middle" style={{ fontSize: 9, fill: "var(--ink-muted)" }}>
            Supervisor · {model.noteCount} notes
          </text>
        </g>

        {/* panelist nodes */}
        {model.names.map((name, i) => {
          const p = pos.get(name)!;
          const seat = model.seats.get(name)!;
          const r = Math.min(10 + seat.count * 2.2, 19);
          const color = PALETTE[i % PALETTE.length];
          const speaking = live && model.lastSpeaker === name;
          return (
            <g key={name}>
              {speaking && (
                <circle cx={p.x} cy={p.y} r={r + 6} fill="none" stroke={color} strokeWidth={2}>
                  <animate attributeName="r" values={`${r + 3};${r + 10};${r + 3}`} dur="1.1s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.9;0.1;0.9" dur="1.1s" repeatCount="indefinite" />
                </circle>
              )}
              <circle cx={p.x} cy={p.y} r={r} fill={color} opacity={0.9}>
                <title>{`${display(name)} — ${seat.count} statement(s)\n${seat.last}`}</title>
              </circle>
              <text
                x={p.x}
                y={p.y + r + 12}
                textAnchor="middle"
                style={{ fontSize: 9.5, fontWeight: 600, fill: "var(--ink)" }}
              >
                {display(name)}
              </text>
              <text x={p.x} y={p.y + 3} textAnchor="middle" style={{ fontSize: 8.5, fontWeight: 700, fill: "#0d1117" }}>
                {seat.count}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="px-2 pb-1 text-[10px]" style={{ color: "var(--ink-muted)" }}>
        Mesh debate: every seat sees and challenges the whole table — amber arcs are challenges,
        dashed spokes are the supervisor&apos;s notes. Node size = participation.
      </p>
    </div>
  );
}
