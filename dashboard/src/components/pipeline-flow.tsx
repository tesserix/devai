"use client";

import { clsx } from "clsx";
import { useEffect, useMemo, useState } from "react";
import { api, type BlueprintGraph, type BlueprintGraphNode } from "@/lib/api";

/**
 * Data-driven pipeline flow.
 *
 * Renders ANY blueprint as an SVG DAG straight from the blueprint-graph
 * endpoint (`GET /api/pipeline/blueprints/{name}`). Lanes become columns,
 * stages become nodes, `depends_on` becomes edges (dashed + labeled when the
 * target stage carries a `condition:`), and stages that share a topo level
 * (`parallel_group`) get a dashed group box. There are no hardcoded stages —
 * a new or UI-authored blueprint shows up here with no code change.
 *
 * Live status overlays onto the static graph: `currentStage` is matched
 * against each node's `name`/`stage` so the active node rings, prior nodes
 * go green, and a failed run reddens the current node.
 */

interface PipelineFlowProps {
  /** Active stage. Matched against node.name or node.stage. "" / "failed" handled. */
  currentStage: string;
  /** agentKey → seconds, surfaced under the matching node. */
  agentTimings?: Record<string, number>;
  /** Which blueprint to render. Defaults to the ALM pipeline. */
  blueprint?: string;
}

// Layout constants (SVG user units).
const PAD_X = 28;
const HEADER_H = 34;
const COL_W = 210;
const NODE_W = 168;
const NODE_H = 48;
const ROW_H = 70;
const GROUP_PAD = 10;

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

interface Placed extends BlueprintGraphNode {
  col: number;
  row: number;
  cx: number;
  cy: number;
  x: number;
  y: number;
}

export function PipelineFlow({
  currentStage,
  agentTimings = {},
  blueprint = "alm-pipeline",
}: PipelineFlowProps) {
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setError("");
    api
      .getBlueprintGraph(blueprint)
      .then((g) => !cancelled && setGraph(g))
      .catch((e) => !cancelled && setError(String(e?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [blueprint]);

  const layout = useMemo(() => {
    if (!graph) return null;

    // Lane order: declared lanes first, then any lane that appears on a node
    // but wasn't declared (keeps unknown-lane blueprints rendering).
    const lanes = [...graph.lanes];
    for (const n of graph.nodes) {
      const lane = n.lane || "_";
      if (!lanes.includes(lane)) lanes.push(lane);
    }
    const laneIdx: Record<string, number> = {};
    lanes.forEach((l, i) => (laneIdx[l] = i));

    // Place each node: column = lane, row = running counter within the lane
    // following the blueprint's (topo-ordered) node array.
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

    // Parallel-group bounding boxes (dashed) for visual grouping.
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
  }, [graph]);

  // Resolve which node is "active" and derive done/pending from node order.
  const activeIdx = useMemo(() => {
    if (!graph || !currentStage) return -1;
    return graph.nodes.findIndex(
      (n) => n.name === currentStage || n.stage === currentStage,
    );
  }, [graph, currentStage]);
  const isFailedRun = currentStage === "failed";

  if (error) {
    return (
      <div className="w-full px-3 py-6 text-sm text-gray-500 dark:text-gray-400">
        Pipeline flow unavailable for{" "}
        <span className="font-mono">{blueprint}</span> — the blueprint runtime
        may be disabled. <span className="text-gray-400">({error})</span>
      </div>
    );
  }
  if (!graph || !layout) {
    return (
      <div className="w-full px-3 py-6 text-sm text-gray-400 dark:text-gray-500 animate-pulse">
        Loading {blueprint} flow…
      </div>
    );
  }

  const { lanes, laneIdx, placed, byName, width, height, groupBoxes } = layout;

  function statusOf(idx: number): "done" | "active" | "failed" | "pending" {
    if (activeIdx < 0) return "pending";
    if (idx === activeIdx) return isFailedRun ? "failed" : "active";
    return idx < activeIdx ? "done" : "pending";
  }

  return (
    <div className="w-full overflow-x-auto">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="text-gray-700 dark:text-gray-200"
        role="img"
        aria-label={`${graph.title} pipeline flow`}
      >
        {/* Lane headers + faint column separators */}
        {lanes.map((lane, i) => {
          const x = PAD_X + i * COL_W;
          const color = laneColor(lane, i);
          if (lane === "_") return null;
          return (
            <g key={`lane-${lane}`}>
              <text
                x={x + COL_W / 2}
                y={18}
                textAnchor="middle"
                className="fill-gray-500 dark:fill-gray-400"
                style={{ fontSize: 11, fontWeight: 600, letterSpacing: 0.4 }}
              >
                {lane.toUpperCase()}
              </text>
              <rect
                x={x + COL_W / 2 - 16}
                y={24}
                width={32}
                height={2}
                rx={1}
                fill={color}
                opacity={0.55}
              />
            </g>
          );
        })}

        {/* Parallel-group boxes (drawn behind nodes) */}
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
            className="stroke-gray-300 dark:stroke-gray-600"
            strokeWidth={1}
          />
        ))}

        {/* Edges */}
        {graph.edges.map((e, i) => {
          const a = byName[e.from];
          const b = byName[e.to];
          if (!a || !b) return null;
          const fromIdx = graph.nodes.findIndex((n) => n.name === e.from);
          const traversed = activeIdx > 0 && fromIdx >= 0 && fromIdx < activeIdx;
          const midX = (a.cx + b.cx) / 2;
          const d = `M ${a.cx} ${a.cy} C ${midX} ${a.cy}, ${midX} ${b.cy}, ${b.cx} ${b.cy}`;
          return (
            <g key={`edge-${i}`}>
              <path
                d={d}
                fill="none"
                strokeWidth={1.5}
                strokeDasharray={e.kind === "conditional" ? "5 4" : undefined}
                className={clsx(
                  traversed
                    ? "stroke-green-400 dark:stroke-green-600"
                    : "stroke-gray-300 dark:stroke-gray-600",
                )}
              />
              {e.kind === "conditional" && e.label && (
                <text
                  x={midX}
                  y={(a.cy + b.cy) / 2 - 3}
                  textAnchor="middle"
                  className="fill-gray-400 dark:fill-gray-500"
                  style={{ fontSize: 8 }}
                >
                  {e.label.length > 22 ? `${e.label.slice(0, 21)}…` : e.label}
                </text>
              )}
            </g>
          );
        })}

        {/* Nodes */}
        {placed.map((n, idx) => {
          const status = statusOf(idx);
          const color = laneColor(n.lane || "_", laneIdx[n.lane || "_"]);
          const timing = n.agent ? agentTimings[n.agent] : undefined;

          const fillClass =
            status === "done"
              ? "fill-green-50 dark:fill-green-950"
              : status === "active"
                ? "fill-indigo-50 dark:fill-indigo-950"
                : status === "failed"
                  ? "fill-red-50 dark:fill-red-950"
                  : "fill-white dark:fill-gray-800";
          const strokeColor =
            status === "done"
              ? "#10b981"
              : status === "active"
                ? "#6366f1"
                : status === "failed"
                  ? "#ef4444"
                  : color;

          return (
            <g key={n.name}>
              {status === "active" && (
                <rect
                  x={n.x - 3}
                  y={n.y - 3}
                  width={NODE_W + 6}
                  height={NODE_H + 6}
                  rx={11}
                  fill="none"
                  stroke="#6366f1"
                  strokeWidth={1}
                  opacity={0.35}
                />
              )}
              <rect
                x={n.x}
                y={n.y}
                width={NODE_W}
                height={NODE_H}
                rx={9}
                className={fillClass}
                stroke={strokeColor}
                strokeWidth={status === "pending" ? 1.5 : 2}
              />
              {/* lane accent bar */}
              <rect x={n.x} y={n.y} width={4} height={NODE_H} rx={2} fill={color} />

              {/* title */}
              <text
                x={n.x + 14}
                y={n.y + 19}
                className="fill-gray-800 dark:fill-gray-100"
                style={{ fontSize: 11.5, fontWeight: 600 }}
              >
                {n.title.length > 20 ? `${n.title.slice(0, 19)}…` : n.title}
              </text>
              {/* sub-line: agent / type, + timing */}
              <text
                x={n.x + 14}
                y={n.y + 34}
                className="fill-gray-400 dark:fill-gray-500"
                style={{ fontSize: 9 }}
              >
                {(n.agent || n.type) +
                  (timing !== undefined ? ` · ${timing.toFixed(1)}s` : "")}
              </text>

              {/* gate badge */}
              {n.gate && (
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
              {status === "done" && (
                <path
                  d={`M ${n.x + NODE_W - 20} ${n.y + 12} l 3 3 l 6 -7`}
                  fill="none"
                  stroke="#10b981"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
