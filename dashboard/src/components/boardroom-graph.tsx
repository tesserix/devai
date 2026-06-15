"use client";

import { useMemo } from "react";
import { GraphDefs, EDGE } from "@/components/graph-defs";

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
  to_agent?: string;
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
    // Direct routing edges: an explicit from → to (non-broadcast) message is a
    // real exchange — draw it as an arrow even without a name mention in text,
    // so non-debate stages (where agents message each other directly) still
    // show prominent connections.
    for (const m of messages) {
      const from = m.from_agent || "";
      const to = m.to_agent || "";
      if (!from || from === "supervisor" || from === to) continue;
      if (!to || to === "boardroom" || to === "supervisor") continue;
      if (!seats.has(from) || !seats.has(to)) continue;
      edges.push([from, to]);
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

  // Directed challenge edges (challenger → challenged). Keep BOTH directions
  // when present — they bend to opposite sides so the arrows never overlap.
  const directedEdges = [...new Set(model.edges.map(([a, b]) => `${a}>${b}`))].map(
    (k) => k.split(">") as [string, string],
  );
  const seatRadius = (name: string) => Math.min(10 + (model.seats.get(name)?.count ?? 1) * 2.2, 19);

  return (
    <div className="rounded-lg p-2" style={{ background: "var(--surface-muted)" }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Boardroom debate graph">
        <GraphDefs idPrefix="br" />
        {/* spokes: each seat reports to the supervisor — solid directional
            arrow (seat → SUP) so the flow is always clearly visible, even when
            there are no seat-to-seat challenges. */}
        {model.names.map((name) => {
          const p = pos.get(name)!;
          const r = seatRadius(name);
          const dx = cx - p.x;
          const dy = cy - p.y;
          const d = Math.hypot(dx, dy) || 1;
          const ux = dx / d;
          const uy = dy / d;
          const x1 = p.x + ux * (r + 1);
          const y1 = p.y + uy * (r + 1);
          const x2 = cx - ux * (20 + 8); // 20 = supervisor radius, 8 = arrow gap
          const y2 = cy - uy * (20 + 8);
          return (
            <line
              key={`spoke-${name}`}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke={EDGE.spoke}
              strokeWidth={2}
              opacity={0.9}
              markerEnd="url(#br-arrow)"
            >
              {live && (
                <animate attributeName="opacity" values="1;0.5;1" dur="2.4s" repeatCount="indefinite" />
              )}
            </line>
          );
        })}

        {/* directed challenge arcs: challenger → challenged (arrowhead at target) */}
        {directedEdges.map(([a, b]) => {
          const pa = pos.get(a);
          const pb = pos.get(b);
          if (!pa || !pb) return null;
          const ra = seatRadius(a);
          const rb = seatRadius(b);
          const dx = pb.x - pa.x;
          const dy = pb.y - pa.y;
          const d = Math.hypot(dx, dy) || 1;
          const ux = dx / d;
          const uy = dy / d;
          const bend = 16;
          // Control point bent perpendicular to the line; opposing edges (b→a)
          // bend the other way, so two-way challenges read as separate arrows.
          const mx = (pa.x + pb.x) / 2 - uy * bend;
          const my = (pa.y + pb.y) / 2 + ux * bend;
          const x1 = pa.x + ux * (ra + 1);
          const y1 = pa.y + uy * (ra + 1);
          const x2 = pb.x - ux * (rb + 7);
          const y2 = pb.y - uy * (rb + 7);
          return (
            <path
              key={`${a}>${b}`}
              d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
              fill="none"
              stroke={EDGE.challenge}
              strokeWidth={2}
              opacity={0.95}
              markerEnd="url(#br-arrow)"
            >
              {live && (
                <animate attributeName="opacity" values="1;0.5;1" dur="2.2s" repeatCount="indefinite" />
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
          <circle
            cx={cx}
            cy={cy}
            r={20}
            fill="#8b5cf6"
            stroke="rgba(255,255,255,0.28)"
            strokeWidth={1}
            opacity={0.96}
            filter="url(#br-shadow)"
          />
          <ellipse cx={cx} cy={cy - 7} rx={12} ry={7} fill="rgba(255,255,255,0.28)" style={{ pointerEvents: "none" }} />
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
              <circle
                cx={p.x}
                cy={p.y}
                r={r}
                fill={color}
                stroke="rgba(255,255,255,0.22)"
                strokeWidth={1}
                opacity={0.95}
                filter="url(#br-shadow)"
              >
                <title>{`${display(name)} — ${seat.count} statement(s)\n${seat.last}`}</title>
              </circle>
              <ellipse
                cx={p.x}
                cy={p.y - r * 0.32}
                rx={r * 0.6}
                ry={r * 0.38}
                fill="rgba(255,255,255,0.28)"
                style={{ pointerEvents: "none" }}
              />
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
        Every seat reports to the supervisor (indigo arrows); amber arrows are seat-to-seat
        challenges. Node size = participation.
      </p>
    </div>
  );
}
