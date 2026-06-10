"use client";

/**
 * Dependency-free SVG charts.
 *
 * The dashboards deliberately ship no charting library (recharts/tremor) —
 * everything is semantic markup + design tokens. These primitives keep that
 * promise: small, themeable (stroke/fill read CSS vars), and SSR-safe. Used by
 * the /analytics page; reusable anywhere.
 */

import { useId } from "react";

type Pt = { label: string; value: number };

// ── Line chart ────────────────────────────────────────────────────────
// Multiple series over a shared x-axis (dates). Smooth-ish polyline, soft
// area fill under the first series, sparse x labels.
export function LineChart({
  series,
  height = 180,
  formatY,
}: {
  series: { name: string; color: string; points: Pt[] }[];
  height?: number;
  formatY?: (n: number) => string;
}) {
  const gid = useId();
  const all = series.flatMap((s) => s.points);
  if (all.length === 0) return <Empty height={height} />;

  const labels = series[0]?.points.map((p) => p.label) ?? [];
  const n = Math.max(1, labels.length - 1);
  const maxV = Math.max(1, ...all.map((p) => p.value));
  const W = 100; // viewBox units (preserveAspectRatio none → stretches to width)
  const padTop = 6;
  const padBottom = 16;
  const innerH = height - padTop - padBottom;

  const x = (i: number) => (n === 0 ? 0 : (i / n) * W);
  const y = (v: number) => padTop + innerH - (v / maxV) * innerH;

  return (
    <div className="w-full" style={{ height }}>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        width="100%"
        height={height}
        role="img"
      >
        {/* gridlines */}
        {[0.25, 0.5, 0.75, 1].map((g) => (
          <line
            key={g}
            x1={0}
            x2={W}
            y1={padTop + innerH - g * innerH}
            y2={padTop + innerH - g * innerH}
            stroke="var(--border-subtle)"
            strokeWidth={0.3}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {series.map((s, si) => {
          const pts = s.points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ");
          return (
            <g key={s.name}>
              {si === 0 && (
                <>
                  <linearGradient id={`${gid}-fill`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={s.color} stopOpacity={0.22} />
                    <stop offset="100%" stopColor={s.color} stopOpacity={0} />
                  </linearGradient>
                  <polygon
                    points={`0,${padTop + innerH} ${pts} ${W},${padTop + innerH}`}
                    fill={`url(#${gid}-fill)`}
                  />
                </>
              )}
              <polyline
                points={pts}
                fill="none"
                stroke={s.color}
                strokeWidth={1.6}
                vectorEffect="non-scaling-stroke"
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            </g>
          );
        })}
      </svg>
      <div className="flex justify-between mt-1" style={{ fontSize: 10, color: "var(--ink-muted)" }}>
        <span>{labels[0]}</span>
        {labels.length > 2 && <span>{labels[Math.floor(labels.length / 2)]}</span>}
        <span>{labels[labels.length - 1]}</span>
      </div>
      <div className="flex gap-3 mt-1" style={{ fontSize: 11 }}>
        {series.map((s) => (
          <span key={s.name} className="inline-flex items-center gap-1" style={{ color: "var(--ink-soft)" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, display: "inline-block" }} />
            {s.name}
          </span>
        ))}
        {formatY && <span style={{ marginLeft: "auto", color: "var(--ink-muted)" }}>peak {formatY(maxV)}</span>}
      </div>
    </div>
  );
}

// ── Horizontal bar chart ───────────────────────────────────────────────
export function HBarChart({
  rows,
  formatValue,
  color = "var(--accent)",
}: {
  rows: Pt[];
  formatValue?: (n: number) => string;
  color?: string;
}) {
  if (rows.length === 0) return <Empty height={120} />;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-2" style={{ fontSize: 12 }}>
          <span className="truncate" style={{ width: 140, color: "var(--ink-soft)" }} title={r.label}>
            {r.label}
          </span>
          <div className="flex-1 rounded" style={{ background: "var(--surface-muted)", height: 14 }}>
            <div
              className="rounded"
              style={{ width: `${(r.value / max) * 100}%`, background: color, height: 14, minWidth: 2 }}
            />
          </div>
          <span className="font-mono tabular-nums" style={{ width: 78, textAlign: "right", color: "var(--ink)" }}>
            {formatValue ? formatValue(r.value) : r.value}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Donut / gauge — single ratio in [0,1] ──────────────────────────────
export function Donut({
  value,
  label,
  size = 120,
}: {
  value: number | null;
  label: string;
  size?: number;
}) {
  const pct = value == null ? 0 : Math.max(0, Math.min(1, value));
  const r = size / 2 - 10;
  const c = 2 * Math.PI * r;
  const dash = c * pct;
  const stroke = pct >= 0.8 ? "var(--ok)" : pct >= 0.5 ? "var(--warn)" : "var(--error)";
  return (
    <div className="flex flex-col items-center" style={{ width: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-muted)" strokeWidth={10} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={value == null ? "var(--surface-muted)" : stroke}
          strokeWidth={10}
          strokeDasharray={`${dash} ${c - dash}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
        <text
          x="50%"
          y="50%"
          dominantBaseline="middle"
          textAnchor="middle"
          style={{ fontSize: 20, fontWeight: 600, fill: "var(--ink-strong)" }}
        >
          {value == null ? "—" : `${Math.round(pct * 100)}%`}
        </text>
      </svg>
      <span style={{ fontSize: 11, color: "var(--ink-muted)", marginTop: 2 }}>{label}</span>
    </div>
  );
}

function Empty({ height }: { height: number }) {
  return (
    <div
      className="flex items-center justify-center rounded"
      style={{ height, color: "var(--ink-muted)", fontSize: 12, background: "var(--surface-muted)" }}
    >
      No data yet
    </div>
  );
}
