"use client";

import { useMemo } from "react";
import type { BlueprintGraph } from "@/lib/api";
import type { RunStateOverlay } from "@/components/blueprint-dag";

/**
 * Blueprint mesh — the pipeline as a force-directed interconnected graph
 * (not lanes). Every stage/agent is a node; depends_on edges weave them into
 * a living network, like a dependency mesh.
 *
 * Layout: a small deterministic force simulation (repulsion + edge springs +
 * centering) runs in useMemo — no physics dependency, stable across renders
 * (seeded by node index). Reads like the reference force graphs.
 *
 * Node color = lane; size grows with connectivity; the live run overlay
 * tints running (amber, pulsing) / done (green) / failed (red) stages.
 */

const LANE_COLORS: Record<string, string> = {
  plan: "#8b5cf6",
  build: "#0ea5e9",
  review: "#f59e0b",
  deploy: "#10b981",
  sre: "#ec4899",
};
const PALETTE = ["#10b981", "#f59e0b", "#8b5cf6", "#0ea5e9", "#ec4899", "#14b8a6", "#f97316"];

function laneColor(lane: string, idx: number): string {
  return LANE_COLORS[lane] ?? PALETTE[idx % PALETTE.length];
}

interface Sim {
  name: string;
  title: string;
  lane: string;
  color: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  deg: number;
}

const W = 900;
const H = 560;

// Deterministic pseudo-random from an integer seed (no Math.random → stable).
function seeded(i: number): number {
  const x = Math.sin(i * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

export function BlueprintMesh({
  graph,
  overlay,
}: {
  graph: BlueprintGraph;
  overlay?: RunStateOverlay;
}) {
  const { nodes, links } = useMemo(() => {
    const lanes = [...graph.lanes];
    for (const n of graph.nodes) if (n.lane && !lanes.includes(n.lane)) lanes.push(n.lane);
    const laneIdx: Record<string, number> = {};
    lanes.forEach((l, i) => (laneIdx[l] = i));

    const idOf = new Map<string, number>();
    graph.nodes.forEach((n, i) => idOf.set(n.name, i));

    // Edges: prefer explicit graph.edges, fall back to depends_on.
    const edges: [number, number][] = [];
    const seen = new Set<string>();
    const addEdge = (a?: string, b?: string) => {
      if (!a || !b) return;
      const ia = idOf.get(a);
      const ib = idOf.get(b);
      if (ia === undefined || ib === undefined || ia === ib) return;
      const key = ia < ib ? `${ia}-${ib}` : `${ib}-${ia}`;
      if (seen.has(key)) return;
      seen.add(key);
      edges.push([ia, ib]);
    };
    for (const e of graph.edges ?? []) addEdge(e.from, e.to);
    for (const n of graph.nodes) for (const d of n.depends_on ?? []) addEdge(d, n.name);

    const deg = new Array(graph.nodes.length).fill(0);
    for (const [a, b] of edges) {
      deg[a]++;
      deg[b]++;
    }

    // Seed positions: spread by lane (x) + golden-angle scatter so the sim
    // starts untangled and converges the same way every render.
    const sim: Sim[] = graph.nodes.map((n, i) => {
      const lx = lanes.length > 1 ? laneIdx[n.lane || lanes[0]] / (lanes.length - 1) : 0.5;
      return {
        name: n.name,
        title: n.title || n.name,
        lane: n.lane || "_",
        color: n.color || laneColor(n.lane || "_", laneIdx[n.lane || lanes[0]] ?? i),
        x: W * (0.15 + 0.7 * lx) + (seeded(i) - 0.5) * 120,
        y: H * (0.1 + 0.8 * seeded(i * 7 + 3)),
        vx: 0,
        vy: 0,
        deg: deg[i],
      };
    });

    // Force simulation — repulsion (Coulomb), edge springs (Hooke), centering.
    const ITER = 220;
    const REPULSE = 5200;
    const SPRING = 0.012;
    const REST = 130;
    const CENTER = 0.006;
    const DAMP = 0.85;
    for (let step = 0; step < ITER; step++) {
      for (let i = 0; i < sim.length; i++) {
        for (let j = i + 1; j < sim.length; j++) {
          let dx = sim[i].x - sim[j].x;
          let dy = sim[i].y - sim[j].y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) {
            d2 = 1;
            dx = seeded(i + j) - 0.5;
            dy = seeded(i * j + 1) - 0.5;
          }
          const f = REPULSE / d2;
          const d = Math.sqrt(d2);
          const fx = (dx / d) * f;
          const fy = (dy / d) * f;
          sim[i].vx += fx;
          sim[i].vy += fy;
          sim[j].vx -= fx;
          sim[j].vy -= fy;
        }
      }
      for (const [a, b] of edges) {
        const dx = sim[b].x - sim[a].x;
        const dy = sim[b].y - sim[a].y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = SPRING * (d - REST);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        sim[a].vx += fx;
        sim[a].vy += fy;
        sim[b].vx -= fx;
        sim[b].vy -= fy;
      }
      for (const s of sim) {
        s.vx += (W / 2 - s.x) * CENTER;
        s.vy += (H / 2 - s.y) * CENTER;
        s.vx *= DAMP;
        s.vy *= DAMP;
        s.x += s.vx;
        s.y += s.vy;
        s.x = Math.max(40, Math.min(W - 40, s.x));
        s.y = Math.max(34, Math.min(H - 34, s.y));
      }
    }

    return { nodes: sim, links: edges };
  }, [graph]);

  const stateOf = (name: string) => overlay?.[name]?.state;

  return (
    <div className="w-full overflow-hidden rounded-lg" style={{ background: "var(--surface-2, #0c0f17)" }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 600 }}>
        {/* edges */}
        {links.map(([a, b], i) => (
          <line
            key={i}
            x1={nodes[a].x}
            y1={nodes[a].y}
            x2={nodes[b].x}
            y2={nodes[b].y}
            stroke="var(--surface-border, #2a2f3a)"
            strokeWidth={1}
            opacity={0.55}
          />
        ))}
        {/* nodes */}
        {nodes.map((n) => {
          const st = stateOf(n.name);
          const r = 9 + Math.min(10, n.deg * 1.6);
          const running = st === "running";
          const done = st === "done";
          const failed = st === "failed";
          const ring = running ? "#f59e0b" : done ? "#10b981" : failed ? "#ef4444" : "transparent";
          return (
            <g key={n.name}>
              {running && (
                <circle cx={n.x} cy={n.y} r={r + 6} fill="none" stroke="#f59e0b" strokeWidth={1.5} opacity={0.5}>
                  <animate attributeName="r" values={`${r + 3};${r + 11};${r + 3}`} dur="1.6s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.6;0;0.6" dur="1.6s" repeatCount="indefinite" />
                </circle>
              )}
              <circle
                cx={n.x}
                cy={n.y}
                r={r}
                fill={n.color}
                stroke={ring}
                strokeWidth={ring === "transparent" ? 0 : 2.5}
                opacity={failed ? 0.9 : 1}
              />
              <text
                x={n.x}
                y={n.y + r + 12}
                textAnchor="middle"
                fontSize={10}
                fill="var(--ink-soft, #9aa3b2)"
                style={{ pointerEvents: "none" }}
              >
                {n.title.length > 22 ? n.title.slice(0, 20) + "…" : n.title}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="flex flex-wrap gap-3 px-3 py-2 text-[11px]" style={{ color: "var(--ink-soft)" }}>
        {[...new Set(nodes.map((n) => n.lane))].filter((l) => l !== "_").map((lane, i) => (
          <span key={lane} className="inline-flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full" style={{ background: laneColor(lane, i) }} />
            {lane}
          </span>
        ))}
      </div>
    </div>
  );
}
