"use client";

/**
 * /logs — Logging & SLOs.
 *
 * Three tabs:
 *   - Live logs   — the api service's in-process log ring (auto-refresh,
 *                   level floor + text filter). A "now" view, not history.
 *   - SLOs        — availability / latency / error-rate burn vs configured
 *                   targets (Prometheus) + incident MTTR + per-app reliability
 *                   (SRE tables).
 *   - Archive     — the GCS log archive: bucket, lifecycle tiers, purge
 *                   policy, and a live object listing when creds permit.
 *
 * Theme: design tokens only, matching /analytics.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Pause,
  Play,
  RefreshCw,
  ScrollText,
  Target,
  XCircle,
} from "lucide-react";
import {
  api,
  type LogArchive,
  type LogEntry,
  type LogsResponse,
  type SLOReport,
} from "@/lib/api";
import { Breadcrumbs } from "@/components/breadcrumbs";

type Tab = "live" | "slo" | "archive";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
const SLO_WINDOWS = [1, 7, 30];

const LEVEL_COLOR: Record<string, string> = {
  DEBUG: "var(--ink-muted)",
  INFO: "var(--info-ink)",
  WARNING: "var(--warn-ink)",
  ERROR: "var(--error-ink)",
  CRITICAL: "var(--error-ink)",
};

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function fmtBytes(n: number): string {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GiB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MiB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KiB`;
  return `${n} B`;
}
function fmtSeconds(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

export default function LogsPage() {
  const [tab, setTab] = useState<Tab>("live");

  return (
    <div className="p-7 space-y-6">
      <Breadcrumbs items={[{ label: "Platform", href: "/" }, { label: "Logs & SLOs" }]} />
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Platform observability</div>
          <h1
            className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
            style={{ color: "var(--ink-strong)" }}
          >
            <ScrollText className="w-5 h-5" style={{ color: "var(--accent)" }} /> Logging &amp; SLOs
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
            Live service logs, SLA/SLO scorecards against configured targets, and the GCS log archive with its
            cost-tiered lifecycle.
          </p>
        </div>
        <div className="seg" role="tablist">
          <button data-active={tab === "live"} onClick={() => setTab("live")}>
            Live logs
          </button>
          <button data-active={tab === "slo"} onClick={() => setTab("slo")}>
            SLOs
          </button>
          <button data-active={tab === "archive"} onClick={() => setTab("archive")}>
            Archive
          </button>
        </div>
      </header>

      {tab === "live" && <LiveLogs />}
      {tab === "slo" && <SLOView />}
      {tab === "archive" && <ArchiveView />}
    </div>
  );
}

// ── Live logs ─────────────────────────────────────────────────────────

function LiveLogs() {
  const [level, setLevel] = useState<string>("INFO");
  const [q, setQ] = useState("");
  const [data, setData] = useState<LogsResponse | null>(null);
  const [paused, setPaused] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.analytics.logs({ limit: 300, level, q }));
    } catch {
      setData(null);
    }
  }, [level, q]);

  useEffect(() => {
    load();
    if (paused) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load, paused]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "nearest" });
  }, [data]);

  const stats = data?.stats;
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="seg" role="group" aria-label="Minimum level">
          {LEVELS.map((l) => (
            <button key={l} data-active={level === l} onClick={() => setLevel(l)}>
              {l}
            </button>
          ))}
        </div>
        <input
          className="field"
          style={{ width: 260 }}
          placeholder="Filter message / logger…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button className="btn-secondary" onClick={() => setPaused((p) => !p)}>
          {paused ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
          {paused ? "Resume" : "Pause"}
        </button>
        <button className="btn-secondary" onClick={load}>
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </button>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-muted)" }}>
          {stats ? `${stats.buffered} buffered · auto-refresh ${paused ? "paused" : "5s"}` : "…"}
        </span>
      </div>

      <div
        className="panel font-mono"
        style={{ padding: 0, maxHeight: 560, overflowY: "auto", fontSize: 12, lineHeight: 1.7 }}
      >
        {(data?.entries ?? []).length === 0 && (
          <div style={{ padding: 24, color: "var(--ink-muted)", textAlign: "center" }}>
            {data?.enabled === false ? "Log ring not enabled on this service." : "No log entries match."}
          </div>
        )}
        {(data?.entries ?? []).map((e: LogEntry, i: number) => (
          <div
            key={`${e.ts}-${i}`}
            className="flex gap-3 px-4 py-0.5"
            style={{ borderBottom: "1px solid var(--border-subtle)" }}
          >
            <span style={{ color: "var(--ink-muted)", flexShrink: 0 }}>{fmtTime(e.ts)}</span>
            <span style={{ color: LEVEL_COLOR[e.level] ?? "var(--ink)", width: 64, flexShrink: 0, fontWeight: 600 }}>
              {e.level}
            </span>
            <span style={{ color: "var(--ink-muted)", width: 200, flexShrink: 0 }} className="truncate" title={e.logger}>
              {e.logger}
            </span>
            <span style={{ color: "var(--ink)", wordBreak: "break-word" }}>
              {e.message}
              {e.exc && <span style={{ color: "var(--error-ink)" }}> — {e.exc}</span>}
            </span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <p className="text-xs" style={{ color: "var(--ink-muted)" }}>
        Live view of the API service&apos;s in-process ring buffer. Durable history lives in the GCS archive (Archive
        tab).
      </p>
    </section>
  );
}

// ── SLOs ──────────────────────────────────────────────────────────────

function SLOView() {
  const [days, setDays] = useState(7);
  const [report, setReport] = useState<SLOReport | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setReport(await api.analytics.slo(days));
    } catch {
      setReport(null);
    }
    setLoading(false);
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  const h = report?.http;
  const inc = report?.incidents ?? {};
  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2">
        <div className="seg" role="group" aria-label="SLO window">
          {SLO_WINDOWS.map((d) => (
            <button key={d} data-active={days === d} onClick={() => setDays(d)}>
              {d}d
            </button>
          ))}
        </div>
        <button className="btn-secondary" onClick={load} disabled={loading}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <SLOCard
          title="Availability"
          actual={h?.availability_pct != null ? `${h.availability_pct}%` : "—"}
          target={`≥ ${report?.targets.availability_pct ?? "—"}%`}
          met={h?.availability_met ?? null}
          sub={
            h?.error_budget_burn_pct != null
              ? `error budget burned: ${h.error_budget_burn_pct}%`
              : "no traffic data in window"
          }
        />
        <SLOCard
          title="Latency p95"
          actual={h?.latency_p95_ms != null ? `${h.latency_p95_ms}ms` : "—"}
          target={`≤ ${report?.targets.latency_p95_ms ?? "—"}ms`}
          met={h?.latency_met ?? null}
          sub={h?.requests != null ? `${Math.round(h.requests)} requests in window` : ""}
        />
        <SLOCard
          title="Error rate"
          actual={h?.error_rate_pct != null ? `${h.error_rate_pct}%` : "—"}
          target={`≤ ${report?.targets.error_rate_pct ?? "—"}%`}
          met={h?.error_rate_met ?? null}
          sub="5xx responses / all responses"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3 flex items-center gap-2">
            <Target className="w-3.5 h-3.5" /> Incident SLO inputs ({days}d)
          </div>
          <div className="grid grid-cols-2 gap-3">
            <MiniStat label="Incidents" value={`${inc.total_incidents ?? 0}`} />
            <MiniStat
              label="Critical"
              value={`${inc.critical_incidents ?? 0}`}
              tone={inc.critical_incidents ? "error" : undefined}
            />
            <MiniStat label="Resolved" value={`${inc.resolved_incidents ?? 0}`} />
            <MiniStat label="Avg MTTR" value={fmtSeconds(inc.avg_mttr_seconds)} />
          </div>
        </div>

        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3">App reliability (SRE, 30d window)</div>
          {(report?.apps ?? []).length === 0 ? (
            <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>No SRE app reliability data.</div>
          ) : (
            <div className="flex flex-col gap-2" style={{ fontSize: 12 }}>
              {(report?.apps ?? []).slice(0, 8).map((a, i) => {
                const name = String(a.name ?? a.app_id ?? `app-${i}`);
                const pct = Number(a.health_pct_7d ?? a.uptime_pct ?? NaN);
                return (
                  <div key={name} className="flex items-center gap-2">
                    <span className="truncate" style={{ width: 180, color: "var(--ink-soft)" }}>
                      {name}
                    </span>
                    <div className="flex-1 rounded" style={{ background: "var(--surface-muted)", height: 12 }}>
                      {!Number.isNaN(pct) && (
                        <div
                          className="rounded"
                          style={{
                            width: `${Math.max(2, Math.min(100, pct))}%`,
                            height: 12,
                            background: pct >= 99 ? "var(--ok)" : pct >= 95 ? "var(--warn)" : "var(--error)",
                          }}
                        />
                      )}
                    </div>
                    <span className="font-mono tabular-nums" style={{ width: 56, textAlign: "right", color: "var(--ink)" }}>
                      {Number.isNaN(pct) ? "—" : `${pct.toFixed(1)}%`}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function SLOCard({
  title,
  actual,
  target,
  met,
  sub,
}: {
  title: string;
  actual: string;
  target: string;
  met: boolean | null;
  sub?: string;
}) {
  const Icon = met == null ? AlertTriangle : met ? CheckCircle2 : XCircle;
  const tone = met == null ? "var(--warn-ink)" : met ? "var(--ok-ink)" : "var(--error-ink)";
  return (
    <div className="panel" style={{ padding: 16 }}>
      <div className="flex items-center justify-between">
        <div className="label-eyebrow">{title}</div>
        <span className={`pill ${met == null ? "pill-warn" : met ? "pill-ok" : "pill-error"}`}>
          {met == null ? "No data" : met ? "Met" : "Breached"}
        </span>
      </div>
      <div className="flex items-baseline gap-2 mt-2">
        <Icon className="w-4 h-4" style={{ color: tone }} />
        <span className="font-mono tabular-nums" style={{ fontSize: 28, fontWeight: 600, color: tone }}>
          {actual}
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-muted)" }}>target {target}</span>
      </div>
      {sub && <div style={{ fontSize: 11, color: "var(--ink-muted)", marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone?: "error" }) {
  return (
    <div className="rounded" style={{ background: "var(--surface-muted)", padding: 12 }}>
      <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>{label}</div>
      <div
        className="font-mono tabular-nums"
        style={{ fontSize: 20, fontWeight: 600, color: tone === "error" ? "var(--error-ink)" : "var(--ink)" }}
      >
        {value}
      </div>
    </div>
  );
}

// ── Archive ───────────────────────────────────────────────────────────

function ArchiveView() {
  const [data, setData] = useState<LogArchive | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.analytics.logsArchive(100));
    } catch {
      setData(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const policy = data?.policy;
  return (
    <section className="space-y-4">
      <div className="panel" style={{ padding: 16 }}>
        <div className="flex items-center justify-between mb-3">
          <div className="label-eyebrow flex items-center gap-2">
            <Archive className="w-3.5 h-3.5" /> GCS log archive
          </div>
          <button className="btn-secondary" onClick={load} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh
          </button>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="rounded" style={{ background: "var(--surface-muted)", padding: 12 }}>
            <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>Bucket</div>
            <div className="font-mono" style={{ fontSize: 14, color: "var(--ink)" }}>
              {policy?.bucket ? `gs://${policy.bucket}` : "not configured"}
            </div>
            <div style={{ fontSize: 11, color: "var(--ink-muted)", marginTop: 8 }}>{policy?.archiver}</div>
            <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>{policy?.purger}</div>
          </div>
          <div className="rounded" style={{ background: "var(--surface-muted)", padding: 12 }}>
            <div style={{ fontSize: 11, color: "var(--ink-muted)", marginBottom: 6 }}>
              Cost-tiered lifecycle (object age)
            </div>
            <div className="flex items-center gap-1" style={{ fontSize: 12 }}>
              <Tier label="Standard" sub="0–2d" color="var(--ok)" />
              <Arrow />
              <Tier label="Nearline" sub="2–7d" color="var(--info)" />
              <Arrow />
              <Tier label="Coldline" sub={`7–${policy?.purge_days ?? 15}d`} color="var(--warn)" />
              <Arrow />
              <Tier label="Deleted" sub={`> ${policy?.purge_days ?? 15}d`} color="var(--error)" />
            </div>
          </div>
        </div>
      </div>

      <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
        <div className="flex items-center justify-between" style={{ padding: "14px 16px 8px" }}>
          <div className="label-eyebrow">Archived objects (newest first)</div>
          <span style={{ fontSize: 11, color: "var(--ink-muted)" }}>
            {data?.listing_available
              ? `${data.objects.length} shown · ${fmtBytes(data.listed_bytes)}`
              : "listing unavailable from this pod — policy still enforced by the bucket"}
          </span>
        </div>
        <table className="tbl w-full" style={{ fontSize: 12 }}>
          <thead>
            <tr>
              <th style={{ padding: "8px 16px", textAlign: "left" }}>Object</th>
              <th style={{ padding: "8px 16px", textAlign: "right" }}>Size</th>
              <th style={{ padding: "8px 16px", textAlign: "right" }}>Class</th>
              <th style={{ padding: "8px 16px", textAlign: "right" }}>Created</th>
            </tr>
          </thead>
          <tbody>
            {(data?.objects ?? []).length === 0 && (
              <tr>
                <td colSpan={4} style={{ padding: 20, color: "var(--ink-muted)", textAlign: "center" }}>
                  No archived objects visible yet — the archive CronJob runs every 6 hours.
                </td>
              </tr>
            )}
            {(data?.objects ?? []).map((o) => (
              <tr key={o.key}>
                <td className="font-mono truncate" style={{ padding: "6px 16px", color: "var(--ink)", maxWidth: 480 }} title={o.key}>
                  {o.key}
                </td>
                <td className="font-mono tabular-nums" style={{ padding: "6px 16px", textAlign: "right", color: "var(--ink-soft)" }}>
                  {fmtBytes(o.size)}
                </td>
                <td style={{ padding: "6px 16px", textAlign: "right" }}>
                  <span className="pill">{o.storage_class || "STANDARD"}</span>
                </td>
                <td className="font-mono tabular-nums" style={{ padding: "6px 16px", textAlign: "right", color: "var(--ink-soft)" }}>
                  {o.updated ? new Date(o.updated * 1000).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Tier({ label, sub, color }: { label: string; sub: string; color: string }) {
  return (
    <span
      className="rounded px-2 py-1"
      style={{ background: "var(--surface)", border: `1px solid var(--border-subtle)`, borderLeft: `3px solid ${color}` }}
    >
      <span style={{ color: "var(--ink)", fontWeight: 600 }}>{label}</span>{" "}
      <span style={{ color: "var(--ink-muted)" }}>{sub}</span>
    </span>
  );
}

function Arrow() {
  return <span style={{ color: "var(--ink-muted)" }}>→</span>;
}
