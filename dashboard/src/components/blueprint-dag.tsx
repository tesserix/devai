"use client";

/**
 * BlueprintDAG — the one canonical DAG renderer.
 *
 * PURE PRESENTATIONAL. It takes a BlueprintGraph (lanes / nodes / edges, the
 * render-ready shape from GET /api/pipeline/blueprints/{name}) and an optional
 * run-state overlay, and draws an SVG DAG with:
 *
 *   - lanes as columns, stages as nodes, depends_on as edges,
 *   - per-stage state coloring (pending / running / done / failed / skipped /
 *     gate-pending) driven entirely by the `overlay` prop,
 *   - gate markers on approval stages, with inline Approve / Reject affordances
 *     for any stage in the gate-pending state (when onApprove/onReject given),
 *   - optional per-stage durations.
 *
 * Two consumers (per the foundation spec):
 *   1. Run detail (DASH-7) — pass the live overlay so the DAG fills in.
 *   2. Blueprint preview on the Workflows home — pass no overlay for a static
 *      "what this blueprint does" diagram.
 *
 * It deliberately does NOT fetch — the parent owns data + polling/SSE. That is
 * what makes it reusable and testable.
 */

import { useMemo } from "react";
import { clsx } from "clsx";
import { Check, ShieldQuestion } from "lucide-react";
import type { BlueprintGraph, BlueprintGraphNode } from "@/lib/api";
import type { RunState } from "@/components/run-state-badge";

export interface StageOverlay {
  /** Canonical state for this stage. */
  state: RunState;
  /** Optional duration in seconds, shown under the node. */
  durationSeconds?: number;
}

/** name → overlay. Stages absent from the map render as `pending`. */
export type RunStateOverlay = Record<string, StageOverlay>;

interface BlueprintDAGProps {
  graph: BlueprintGraph;
  /** name → {state, durationSeconds}. Omit for a static blueprint preview. */
  overlay?: RunStateOverlay;
  /** Called when a gate-pending stage is approved. Enables the inline button. */
  onApprove?: (stageName: string) => void;
  /** Called when a gate-pending stage is rejected. Enables the inline button. */
  onReject?: (stageName: string) => void;
  /** Compact node sizing for tight cards (blueprint preview). */
  dense?: boolean;
  className?: string;
}

// Lane → accent color. Falls back to a palette by lane index for unknown lanes.
const LANE_COLORS: Record<string, string> = {
  plan: "#0ea5e9",
  build: "#f59e0b",
  review: "#8b5cf6",
  deploy: "#10b981",
  discovery: "#0ea5e9",
  monitor: "#f59e0b",
  respond: "#8b5cf6",
};
const PALETTE = ["#0ea5e9", "#f59e0b", "#8b5cf6", "#10b981", "#ec4899", "#14b8a6"];

function laneColor(lane: string, idx: number): string {
  return LANE_COLORS[lane] ?? PALETTE[idx % PALETTE.length];
}

// State → SVG fill/stroke. Uses literal colors (SVG can't read CSS vars on
// every attr reliably across browsers); these align with the globals.css
// state palette intent (moss/amber/oxblood family).
const STATE_STROKE: Record<RunState, string> = {
  pending: "#a6a9af",
  queued: "#a6a9af",
  running: "#6366f1",
  done: "#10b981",
  failed: "#ef4444",
  skipped: "#c5c7cc",
  paused: "#f59e0b",
  cancelled: "#a6a9af",
  "gate-pending": "#f59e0b",
};

interface Placed extends BlueprintGraphNode {
  col: number;
  row: number;
  x: number;
  y: number;
  cx: number;
  cy: number;
}

export function BlueprintDAG({
  graph,
  overlay,
  onApprove,
  onReject,
  dense = false,
  className,
}: BlueprintDAGProps) {
  const PAD_X = 28;
  const HEADER_H = 34;
  const COL_W = dense ? 180 : 210;
  const NODE_W = dense ? 150 : 168;
  const NODE_H = dense ? 42 : 48;
  const ROW_H = dense ? 60 : 74;
  const GROUP_PAD = 10;

  const layout = useMemo(() => {
    const lanes = [...graph.lanes];
    for (const n of graph.nodes) {
      const lane = n.lane || "_";
      if (!lanes.includes(lane)) lanes.push(lane);
    }
    const laneIdx: Record<string, number> = {};
    lanes.forEach((l, i) => (laneIdx[l] = i));

    const rowByLane: Record<string, number> = {};
    const placed: Placed[] = graph.nodes.map((n) => {
      const lane = n.lane || "_";
      const col = laneIdx[lane];
      const row = rowByLane[lane] ?? 0;
      rowByLane[lane] = row + 1;
      const x = PAD_X + col * COL_W + (COL_W - NODE_W) / 2;
      const y = HEADER_H + row * ROW_H;
      return { ...n, col, row, x, y, cx: x + NODE_W / 2, cy: y + NODE_H / 2 };
    });
    const byName: Record<string, Placed> = {};
    placed.forEach((p) => (byName[p.name] = p));

    const maxRows = Math.max(1, ...Object.values(rowByLane));
    const width = PAD_X * 2 + lanes.length * COL_W;
    const height = HEADER_H + maxRows * ROW_H + 12;

    const groups: Record<string, Placed[]> = {};
    for (const p of placed) {
      if (p.parallel_group) (groups[p.parallel_group] ??= []).push(p);
    }
    const groupBoxes = Object.entries(groups)
      .filter(([, members]) => members.length > 1)
      .map(([gid, members]) => {
        const minX = Math.min(...members.map((m) => m.x)) - GROUP_PAD;
        const minY = Math.min(...members.map((m) => m.y)) - GROUP_PAD;
        const maxX = Math.max(...members.map((m) => m.x + NODE_W)) + GROUP_PAD;
        const maxY = Math.max(...members.map((m) => m.y + NODE_H)) + GROUP_PAD;
        return { gid, x: minX, y: minY, w: maxX - minX, h: maxY - minY };
      });

    return { lanes, laneIdx, placed, byName, width, height, groupBoxes };
  }, [graph, COL_W, NODE_W, NODE_H, ROW_H]);

  const { lanes, laneIdx, placed, byName, width, height, groupBoxes } = layout;

  function stateOf(name: string): RunState {
    return overlay?.[name]?.state ?? "pending";
  }

  // An edge is "traversed" when its source stage is done — colors the path green.
  function edgeTraversed(from: string): boolean {
    return stateOf(from) === "done";
  }

  return (
    <div className={clsx("w-full overflow-x-auto", className)}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${graph.title} stage graph`}
      >
        {/* Lane headers */}
        {lanes.map((lane, i) => {
          if (lane === "_") return null;
          const x = PAD_X + i * COL_W;
          const color = laneColor(lane, i);
          return (
            <g key={`lane-${lane}`}>
              <text
                x={x + COL_W / 2}
                y={18}
                textAnchor="middle"
                style={{ fontSize: 11, fontWeight: 600, letterSpacing: 0.4, fill: "var(--ink-muted)" }}
              >
                {lane.toUpperCase()}
              </text>
              <rect x={x + COL_W / 2 - 16} y={24} width={32} height={2} rx={1} fill={color} opacity={0.55} />
            </g>
          );
        })}

        {/* Parallel-group boxes */}
        {groupBoxes.map((b) => (
          <rect
            key={`grp-${b.gid}`}
            x={b.x}
            y={b.y}
            width={b.w}
            height={b.h}
            rx={10}
            fill="none"
            strokeDasharray="4 4"
            stroke="var(--border)"
            strokeWidth={1}
          />
        ))}

        {/* Edges */}
        {graph.edges.map((e, i) => {
          const a = byName[e.from];
          const b = byName[e.to];
          if (!a || !b) return null;
          const traversed = edgeTraversed(e.from);
          // Route from the BOTTOM edge of the source to the TOP edge of the
          // target — a top-down flow. The old version drew center-to-center
          // (a.cy → b.cy), so the connector ran straight THROUGH both node
          // boxes and overlapped them. Anchoring to the box edges keeps the
          // line in the gap between rows; the control points sit at the
          // vertical midpoint so same-column edges are a clean straight line
          // and cross-lane edges curve smoothly.
          const ay = a.y + NODE_H; // bottom-center of source
          const by = b.y; // top-center of target
          const midY = (ay + by) / 2;
          const d = `M ${a.cx} ${ay} C ${a.cx} ${midY}, ${b.cx} ${midY}, ${b.cx} ${by}`;
          return (
            <g key={`edge-${i}`}>
              <path
                d={d}
                fill="none"
                strokeWidth={1.5}
                strokeDasharray={e.kind === "conditional" ? "5 4" : undefined}
                stroke={traversed ? "#10b981" : "var(--border)"}
              />
              {e.kind === "conditional" && e.label && (
                <text
                  x={(a.cx + b.cx) / 2 + 6}
                  y={midY - 2}
                  textAnchor="middle"
                  style={{ fontSize: 8, fill: "var(--ink-muted)" }}
                >
                  {e.label.length > 22 ? `${e.label.slice(0, 21)}…` : e.label}
                </text>
              )}
            </g>
          );
        })}

        {/* Nodes */}
        {placed.map((n) => {
          const state = stateOf(n.name);
          const color = laneColor(n.lane || "_", laneIdx[n.lane || "_"]);
          const stroke = STATE_STROKE[state];
          const duration = overlay?.[n.name]?.durationSeconds;
          const isGatePending = state === "gate-pending";
          const skipped = state === "skipped";

          const running = state === "running";
          const done = state === "done";

          return (
            <g key={n.name} opacity={skipped ? 0.5 : 1}>
              {/* RUNNING: an obvious pulsing glow ring so in-progress stages
                  clearly "blink" (opacity + width both animate, fast cadence). */}
              {running && (
                <rect
                  x={n.x - 4}
                  y={n.y - 4}
                  width={NODE_W + 8}
                  height={NODE_H + 8}
                  rx={12}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={2}
                >
                  <animate attributeName="opacity" values="0.75;0.08;0.75" dur="1.05s" repeatCount="indefinite" />
                  <animate attributeName="stroke-width" values="1.5;4;1.5" dur="1.05s" repeatCount="indefinite" />
                </rect>
              )}
              <rect
                x={n.x}
                y={n.y}
                width={NODE_W}
                height={NODE_H}
                rx={9}
                // DONE gets a faint green tint so "completed" reads at a glance.
                fill={done ? "rgba(16,185,129,0.12)" : "var(--surface-raised)"}
                stroke={stroke}
                strokeWidth={running || done || state === "failed" ? 2.25 : state === "pending" || state === "skipped" ? 1.5 : 2}
                strokeDasharray={skipped ? "4 3" : undefined}
              >
                {/* the border itself also pulses while running */}
                {running && (
                  <animate attributeName="stroke-opacity" values="1;0.3;1" dur="1.05s" repeatCount="indefinite" />
                )}
              </rect>
              {/* lane accent bar */}
              <rect x={n.x} y={n.y} width={4} height={NODE_H} rx={2} fill={color} />

              {/* title */}
              <text
                x={n.x + 14}
                y={n.y + 19}
                style={{ fontSize: 11.5, fontWeight: 600, fill: "var(--ink-strong)" }}
              >
                {n.title.length > 20 ? `${n.title.slice(0, 19)}…` : n.title}
              </text>
              {/* sub-line: agent / type + duration */}
              <text x={n.x + 14} y={n.y + 34} style={{ fontSize: 9, fill: "var(--ink-muted)" }}>
                {(n.agent || n.type) + (duration !== undefined ? ` · ${duration.toFixed(1)}s` : "")}
              </text>

              {/* gate marker */}
              {n.gate && !isGatePending && (
                <g>
                  <rect
                    x={n.x + NODE_W - 22}
                    y={n.y + 6}
                    width={16}
                    height={12}
                    rx={3}
                    fill="#f59e0b"
                    opacity={0.85}
                  />
                  <title>Human approval gate</title>
                </g>
              )}

              {/* done check */}
              {state === "done" && (
                <path
                  d={`M ${n.x + NODE_W - 20} ${n.y + 12} l 3 3 l 6 -7`}
                  fill="none"
                  stroke="#10b981"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}

              {/* failed mark */}
              {state === "failed" && (
                <g stroke="#ef4444" strokeWidth={2} strokeLinecap="round">
                  <line x1={n.x + NODE_W - 21} y1={n.y + 8} x2={n.x + NODE_W - 13} y2={n.y + 16} />
                  <line x1={n.x + NODE_W - 13} y1={n.y + 8} x2={n.x + NODE_W - 21} y2={n.y + 16} />
                </g>
              )}

              {/* inline gate actions — only when pending AND handlers given */}
              {isGatePending && (onApprove || onReject) && (
                <foreignObject x={n.x} y={n.y + NODE_H + 4} width={NODE_W} height={26}>
                  <div className="flex items-center gap-1.5">
                    <span
                      className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded"
                      style={{ background: "var(--warn-soft-bg)", color: "var(--warn-ink)", border: "1px solid var(--warn-soft-bd)" }}
                    >
                      <ShieldQuestion className="w-3 h-3" />
                      Gate
                    </span>
                    {onReject && (
                      <button
                        type="button"
                        onClick={() => onReject(n.name)}
                        className="text-[10px] px-1.5 py-0.5 rounded"
                        style={{ color: "var(--error-ink)", background: "var(--error-soft-bg)", border: "1px solid var(--error-soft-bd)" }}
                      >
                        Reject
                      </button>
                    )}
                    {onApprove && (
                      <button
                        type="button"
                        onClick={() => onApprove(n.name)}
                        className="text-[10px] px-1.5 py-0.5 rounded font-medium"
                        style={{ color: "var(--ok-ink)", background: "var(--ok-soft-bg)", border: "1px solid var(--ok-soft-bd)" }}
                      >
                        Approve
                      </button>
                    )}
                  </div>
                </foreignObject>
              )}
            </g>
          );
        })}
      </svg>

      {/* Legend — quiet, only when an overlay is present (i.e. a live run). */}
      {overlay && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 pt-2 text-[11px]" style={{ color: "var(--ink-muted)" }}>
          <LegendDot color="#6366f1" label="Running" />
          <LegendDot color="#10b981" label="Done" />
          <LegendDot color="#ef4444" label="Failed" />
          <LegendDot color="#f59e0b" label="Gate / paused" />
          <LegendDot color="#c5c7cc" label="Skipped" />
          <span className="inline-flex items-center gap-1.5">
            <Check className="w-3 h-3" style={{ color: "#10b981" }} /> stage complete
          </span>
        </div>
      )}
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block w-2 h-2 rounded-full" style={{ background: color }} aria-hidden />
      {label}
    </span>
  );
}
