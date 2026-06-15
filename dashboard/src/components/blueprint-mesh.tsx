"use client";

import { useMemo } from "react";
import type { BlueprintGraph } from "@/lib/api";
import type { RunStateOverlay } from "@/components/blueprint-dag";
import { GraphDefs, EDGE } from "@/components/graph-defs";

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
  const radiusOf = (n: Sim) => 11 + Math.min(11, n.deg * 1.7);

  return (
    <div className="w-full overflow-hidden rounded-lg" style={{ background: "var(--surface-2, #0c0f17)" }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 600 }}>
        <GraphDefs idPrefix="mesh" />

        {/* directed edges: prerequisite → stage, arrow shows execution flow */}
        {links.map(([a, b], i) => {
          const na = nodes[a];
          const nb = nodes[b];
          const dx = nb.x - na.x;
          const dy = nb.y - na.y;
          const d = Math.hypot(dx, dy) || 1;
          const ux = dx / d;
          const uy = dy / d;
          const ra = radiusOf(na);
          const rb = radiusOf(nb);
          const x1 = na.x + ux * (ra + 2);
          const y1 = na.y + uy * (ra + 2);
          const x2 = nb.x - ux * (rb + 7);
          const y2 = nb.y - uy * (rb + 7);
          const traversed = stateOf(na.name) === "done";
          return (
            <line
              key={i}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke={traversed ? EDGE.done : EDGE.flow}
              strokeWidth={2}
              opacity={traversed ? 0.95 : 0.85}
              markerEnd="url(#mesh-arrow)"
            />
          );
        })}

        {/* nodes */}
        {nodes.map((n) => {
          const st = stateOf(n.name);
          const r = radiusOf(n);
          const running = st === "running";
          const done = st === "done";
          const failed = st === "failed";
          const ring = running ? "#f59e0b" : done ? "#10b981" : failed ? "#ef4444" : "";
          const initial = (n.title || n.name).trim().charAt(0).toUpperCase();
          return (
            <g key={n.name}>
              {/* state ring (outside the body so it never hides the fill) */}
              {ring && (
                <circle cx={n.x} cy={n.y} r={r + 3.5} fill="none" stroke={ring} strokeWidth={2.25} opacity={0.95}>
                  {running && (
                    <animate attributeName="opacity" values="1;0.25;1" dur="1.3s" repeatCount="indefinite" />
                  )}
                </circle>
              )}
              {running && (
                <circle cx={n.x} cy={n.y} r={r + 7} fill="none" stroke="#f59e0b" strokeWidth={1.5} opacity={0.5}>
                  <animate attributeName="r" values={`${r + 4};${r + 13};${r + 4}`} dur="1.6s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.55;0;0.55" dur="1.6s" repeatCount="indefinite" />
                </circle>
              )}
              {/* body + thin rim + top sheen → a dimensional "chip" */}
              <circle
                cx={n.x}
                cy={n.y}
                r={r}
                fill={n.color}
                stroke="rgba(255,255,255,0.22)"
                strokeWidth={1}
                opacity={failed ? 0.92 : 1}
                filter="url(#mesh-shadow)"
              />
              <ellipse
                cx={n.x}
                cy={n.y - r * 0.32}
                rx={r * 0.62}
                ry={r * 0.4}
                fill="rgba(255,255,255,0.28)"
                style={{ pointerEvents: "none" }}
              />
              <text
                x={n.x}
                y={n.y + r * 0.34}
                textAnchor="middle"
                fontSize={r * 0.82}
                fontWeight={700}
                fill="#fff"
                style={{ pointerEvents: "none" }}
              >
                {initial}
              </text>
              <text
                x={n.x}
                y={n.y + r + 13}
                textAnchor="middle"
                fontSize={10}
                fontWeight={500}
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
