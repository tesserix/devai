"use client";

/**
 * /analytics — platform observability for the ALM runtime.
 *
 * Restored as a real page (was a DASH-9 "coming soon" stub). Backed by
 * /api/analytics/* — run/stage stats from the live runtime, agent + LLM
 * token/cost from Postgres, and telemetry/OTel-collector health. Every section
 * loads independently and degrades to an empty state, so one missing source
 * never blanks the page.
 *
 * Theme: design tokens only (no raw Tailwind grays); charts are the
 * dependency-free SVG primitives in components/charts.
 */

import { useCallback, useEffect, useState } from "react";
import { Activity, BarChart3, Brain, RefreshCw, Radio, AlertTriangle, CheckCircle2 } from "lucide-react";
import {
  api,
  type AgentStat,
  type AnalyticsSRESummary,
  type AnalyticsSummary,
  type LLMCost,
  type MemoryAnalytics,
  type RunsTimeseriesPoint,
  type StageStat,
  type TelemetryHealth,
} from "@/lib/api";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { Donut, HBarChart, LineChart } from "@/components/charts";

const DAY_OPTIONS = [7, 30, 90];

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(1)}m`;
  const h = m / 60;
  if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}
function fmtUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}
function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [runsTs, setRunsTs] = useState<RunsTimeseriesPoint[]>([]);
  const [stages, setStages] = useState<StageStat[]>([]);
  const [agents, setAgents] = useState<AgentStat[]>([]);
  const [llm, setLlm] = useState<LLMCost | null>(null);
  const [sre, setSre] = useState<AnalyticsSRESummary | null>(null);
  const [tel, setTel] = useState<TelemetryHealth | null>(null);
  const [mem, setMem] = useState<MemoryAnalytics | null>(null);
  const [usage, setUsage] = useState<Awaited<ReturnType<typeof api.analytics.usage>> | null>(null);
  const [evals, setEvals] = useState<Awaited<ReturnType<typeof api.analytics.evals>> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const settle = <T,>(p: Promise<T>, fallback: T): Promise<T> => p.catch(() => fallback);
    const [s, rt, st, ag, lc, sr, th, mm, us, ev] = await Promise.all([
      settle(api.analytics.summary(days), null as AnalyticsSummary | null),
      settle(api.analytics.runsTimeseries(days), [] as RunsTimeseriesPoint[]),
      settle(api.analytics.stages(), [] as StageStat[]),
      settle(api.analytics.agents(days), [] as AgentStat[]),
      settle(api.analytics.llmCost(days), null as LLMCost | null),
      settle(api.analytics.sreSummary(), null as AnalyticsSRESummary | null),
      settle(api.analytics.telemetry(), null as TelemetryHealth | null),
      settle(api.analytics.memory(days), null as MemoryAnalytics | null),
      settle(api.analytics.usage(days), null as Awaited<ReturnType<typeof api.analytics.usage>> | null),
      settle(api.analytics.evals(days), null as Awaited<ReturnType<typeof api.analytics.evals>> | null),
    ]);
    setSummary(s);
    setRunsTs(rt);
    setStages(st);
    setAgents(ag);
    setLlm(lc);
    setSre(sr && Object.keys(sr).length ? sr : null);
    setTel(th);
    setMem(mm);
    setUsage(us);
    setEvals(ev);
    setLoading(false);
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  // Prefer the usage ledger (real cost, all runs) over the Postgres rollup.
  const ledgerCost = usage?.enabled ? usage.summary.cost_usd : 0;
  const ledgerTokens = usage?.enabled ? usage.summary.tokens_in + usage.summary.tokens_out : 0;
  const totalCost = ledgerCost || (llm?.by_model ?? []).reduce((a, m) => a + (m.cost_usd || 0), 0);
  const totalTokens =
    ledgerTokens || (llm?.by_model ?? []).reduce((a, m) => a + (m.tokens_input || 0) + (m.tokens_output || 0), 0);
  const fmtUsd = (n: number | null | undefined) =>
    `$${(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const fmtMs = (n: number | null | undefined) => ((n || 0) >= 1000 ? `${((n || 0) / 1000).toFixed(1)}s` : `${Math.round(n || 0)}ms`);

  return (
    <div className="p-7 space-y-6">
      <Breadcrumbs items={[{ label: "Platform", href: "/" }, { label: "Analytics" }]} />
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Platform observability</div>
          <h1
            className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
            style={{ color: "var(--ink-strong)" }}
          >
            <BarChart3 className="w-5 h-5" style={{ color: "var(--accent)" }} /> Analytics
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
            Pipeline runs, per-agent and LLM token/cost rollups, and live telemetry health. Run stats come from the
            active runtime; agent/LLM cost from execution history; OTel from the telemetry collector.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="seg" role="group" aria-label="Time window">
            {DAY_OPTIONS.map((d) => (
              <button key={d} onClick={() => setDays(d)} data-active={days === d} aria-pressed={days === d}>
                {d}d
              </button>
            ))}
          </div>
          <button onClick={load} className="btn-secondary" disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh
          </button>
        </div>
      </header>

      {/* KPI cards */}
      <section className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
        <Kpi label="Total runs" value={fmtNum(summary?.runs.total)} sub={`${summary?.runs.active ?? 0} active`} />
        <div className="panel flex items-center justify-between" style={{ padding: 14 }}>
          <div>
            <div className="label-eyebrow">Success rate</div>
            <div className="text-xs mt-1" style={{ color: "var(--ink-muted)" }}>
              {summary?.runs.completed ?? 0} ok · {summary?.runs.failed ?? 0} failed
            </div>
          </div>
          <Donut value={summary?.runs.success_rate ?? null} label="" size={80} />
        </div>
        <Kpi label="Avg duration" value={fmtMs(summary?.runs.avg_duration_ms)} sub="per run (terminal)" />
        <Kpi label="LLM spend" value={fmtUsd(totalCost)} sub={`${fmtNum(totalTokens)} tokens`} />
        <Kpi label="Failed runs" value={fmtNum(summary?.runs.failed)} sub={`window ${days}d`} accent="error" />
      </section>

      {/* Telemetry / OTel health */}
      <TelemetryPanel tel={tel} />

      {/* Runs over time + LLM cost over time */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-2">Runs over time</div>
          <LineChart
            height={180}
            series={[
              {
                name: "Completed",
                color: "var(--ok)",
                points: runsTs.map((p) => ({ label: p.date, value: p.completed })),
              },
              {
                name: "Failed",
                color: "var(--error)",
                points: runsTs.map((p) => ({ label: p.date, value: p.failed })),
              },
            ]}
          />
        </div>
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-2">LLM cost over time</div>
          <LineChart
            height={180}
            formatY={(n) => fmtUsd(n)}
            series={[
              {
                name: "Cost (USD)",
                color: "var(--accent)",
                points: (llm?.timeseries ?? []).map((p) => ({ label: p.date, value: p.cost_usd })),
              },
            ]}
          />
        </div>
      </section>

      {/* LLM usage — real cost / tokens / latency per model and per user */}
      {usage?.enabled && (usage.by_model.length > 0 || usage.by_user.length > 0) && (
        <section className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Kpi label="LLM spend" value={fmtUsd(usage.summary.cost_usd)} sub="all runs, est." />
            <Kpi label="LLM calls" value={fmtNum(usage.summary.calls)} sub={`${fmtNum(usage.summary.errors)} errors`} />
            <Kpi
              label="Tokens"
              value={fmtNum(usage.summary.tokens_in + usage.summary.tokens_out)}
              sub={`${fmtNum(usage.summary.tokens_in)} in / ${fmtNum(usage.summary.tokens_out)} out`}
            />
            <Kpi
              label="Avg latency"
              value={fmtMs(usage.summary.calls ? usage.summary.duration_ms / usage.summary.calls : 0)}
              sub="per call"
            />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="panel" style={{ padding: 16 }}>
              <div className="label-eyebrow mb-3">By model — cost · tokens · latency</div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left" style={{ color: "var(--ink-soft)" }}>
                      <th className="pb-2 font-normal">Model</th>
                      <th className="pb-2 font-normal text-right">Calls</th>
                      <th className="pb-2 font-normal text-right">Tokens</th>
                      <th className="pb-2 font-normal text-right">Avg ms</th>
                      <th className="pb-2 font-normal text-right">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_model.map((m) => (
                      <tr key={m.model} className="border-t" style={{ borderColor: "var(--surface-border)" }}>
                        <td className="py-1.5">
                          <span style={{ color: "var(--ink-strong)" }}>{m.model}</span>
                          <span className="ml-1.5 text-xs" style={{ color: "var(--ink-soft)" }}>
                            {m.provider}
                          </span>
                        </td>
                        <td className="py-1.5 text-right">{fmtNum(m.calls)}</td>
                        <td className="py-1.5 text-right">{fmtNum(m.tokens_in + m.tokens_out)}</td>
                        <td className="py-1.5 text-right">{Math.round(m.calls ? m.duration_ms / m.calls : 0)}</td>
                        <td className="py-1.5 text-right" style={{ color: "var(--accent)" }}>
                          {fmtUsd(m.cost_usd)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="panel" style={{ padding: 16 }}>
              <div className="label-eyebrow mb-3">By user — who is spending what</div>
              {usage.by_user.length === 0 ? (
                <p className="text-sm" style={{ color: "var(--ink-soft)" }}>
                  No per-user usage yet. Runs attribute to the user who triggered them.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left" style={{ color: "var(--ink-soft)" }}>
                        <th className="pb-2 font-normal">User</th>
                        <th className="pb-2 font-normal text-right">Calls</th>
                        <th className="pb-2 font-normal text-right">Tokens</th>
                        <th className="pb-2 font-normal text-right">Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {usage.by_user.map((u) => (
                        <tr key={u.user} className="border-t" style={{ borderColor: "var(--surface-border)" }}>
                          <td className="py-1.5" style={{ color: "var(--ink-strong)" }}>
                            {u.user}
                          </td>
                          <td className="py-1.5 text-right">{fmtNum(u.calls)}</td>
                          <td className="py-1.5 text-right">{fmtNum(u.tokens_in + u.tokens_out)}</td>
                          <td className="py-1.5 text-right" style={{ color: "var(--accent)" }}>
                            {fmtUsd(u.cost_usd)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-xs mt-3" style={{ color: "var(--ink-soft)" }}>
                Cost is estimated from each model&apos;s published rate (USD per 1M tokens). Captured for every run.
              </p>
            </div>
          </div>
        </section>
      )}

      {/* Evals — quality scores (pass rate, by evaluator) */}
      <section className="panel" style={{ padding: 16 }}>
        <div className="label-eyebrow mb-3">Quality evals {evals?.scope === "me" ? "· your runs" : ""}</div>
        {!evals || evals.summary.evals === 0 ? (
          <p className="text-sm" style={{ color: "var(--ink-soft)" }}>
            No evals recorded yet. Quality gates (review, security, tests) score each run; scores will
            appear here as runs complete.
          </p>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="grid grid-cols-3 gap-3 lg:col-span-1">
              <Kpi label="Evals" value={fmtNum(evals.summary.evals)} />
              <Kpi label="Pass rate" value={`${Math.round((evals.summary.pass_rate || 0) * 100)}%`} />
              <Kpi label="Avg score" value={(evals.summary.avg_score || 0).toFixed(2)} />
            </div>
            <div className="lg:col-span-2 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left" style={{ color: "var(--ink-soft)" }}>
                    <th className="pb-2 font-normal">Evaluator</th>
                    <th className="pb-2 font-normal text-right">Evals</th>
                    <th className="pb-2 font-normal text-right">Pass rate</th>
                    <th className="pb-2 font-normal text-right">Avg score</th>
                  </tr>
                </thead>
                <tbody>
                  {evals.by_evaluator.map((e) => (
                    <tr key={e.evaluator} className="border-t" style={{ borderColor: "var(--surface-border)" }}>
                      <td className="py-1.5" style={{ color: "var(--ink-strong)" }}>{e.evaluator}</td>
                      <td className="py-1.5 text-right">{fmtNum(e.evals)}</td>
                      <td className="py-1.5 text-right">{Math.round((e.pass_rate || 0) * 100)}%</td>
                      <td className="py-1.5 text-right">{(e.avg_score || 0).toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>

      {/* Agents + Stages */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3">Top agents by LLM cost</div>
          <HBarChart
            color="var(--accent)"
            rows={agents
              .slice()
              .sort((a, b) => b.total_cost_usd - a.total_cost_usd)
              .slice(0, 8)
              .map((a) => ({ label: a.agent_name, value: a.total_cost_usd }))}
            formatValue={(n) => fmtUsd(n)}
          />
        </div>
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3">Slowest stages (avg)</div>
          <HBarChart
            color="var(--info)"
            rows={stages
              .filter((s) => s.avg_duration_ms != null)
              .sort((a, b) => (b.avg_duration_ms ?? 0) - (a.avg_duration_ms ?? 0))
              .slice(0, 8)
              .map((s) => ({ label: s.stage, value: s.avg_duration_ms ?? 0 }))}
            formatValue={(n) => fmtMs(n)}
          />
        </div>
      </section>

      {/* Agent table */}
      <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
        <div className="label-eyebrow" style={{ padding: "14px 16px 8px" }}>
          Agent executions ({days}d)
        </div>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl w-full" style={{ fontSize: 13 }}>
            <thead>
              <tr>
                <Th>Agent</Th>
                <Th right>Runs</Th>
                <Th right>Failures</Th>
                <Th right>Avg</Th>
                <Th right>Tokens in</Th>
                <Th right>Tokens out</Th>
                <Th right>Cost</Th>
              </tr>
            </thead>
            <tbody>
              {agents.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ padding: 16, color: "var(--ink-muted)", textAlign: "center" }}>
                    No agent executions recorded yet.
                  </td>
                </tr>
              )}
              {agents.map((a) => (
                <tr key={a.agent_name}>
                  <td style={{ padding: "8px 16px", color: "var(--ink)" }}>{a.agent_name}</td>
                  <Td>{a.executions}</Td>
                  <Td style={{ color: a.failures ? "var(--error-ink)" : undefined }}>{a.failures}</Td>
                  <Td>{fmtMs(a.avg_duration_ms)}</Td>
                  <Td>{fmtNum(a.tokens_input)}</Td>
                  <Td>{fmtNum(a.tokens_output)}</Td>
                  <Td>{fmtUsd(a.total_cost_usd)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Agentic memory */}
      <MemoryPanel mem={mem} days={days} onChanged={load} />

      {/* By blueprint + SRE strip */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3">By blueprint</div>
          <div className="flex flex-col gap-2">
            {(summary?.by_blueprint ?? []).length === 0 && (
              <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>No runs yet.</div>
            )}
            {(summary?.by_blueprint ?? []).map((b) => (
              <div key={b.blueprint} className="flex items-center gap-2" style={{ fontSize: 12 }}>
                <span className="truncate" style={{ width: 160, color: "var(--ink-soft)" }} title={b.blueprint}>
                  {b.blueprint}
                </span>
                <span className="font-mono tabular-nums" style={{ width: 40, color: "var(--ink)" }}>
                  {b.total}
                </span>
                <div className="flex-1 rounded flex overflow-hidden" style={{ background: "var(--surface-muted)", height: 12 }}>
                  <div style={{ width: `${b.total ? (b.completed / b.total) * 100 : 0}%`, background: "var(--ok)" }} />
                  <div style={{ width: `${b.total ? (b.failed / b.total) * 100 : 0}%`, background: "var(--error)" }} />
                </div>
                <span className="font-mono tabular-nums" style={{ width: 48, textAlign: "right", color: "var(--ink-soft)" }}>
                  {b.success_rate == null ? "—" : `${Math.round(b.success_rate * 100)}%`}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="panel" style={{ padding: 16 }}>
          <div className="label-eyebrow mb-3">SRE summary</div>
          {sre == null ? (
            <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>
              SRE tables not reachable from this service (or empty). The SRE dashboard has the full view.
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="Open incidents" value={fmtNum(sre.open_incidents)} />
              <Stat
                label="Critical"
                value={fmtNum(sre.critical_incidents)}
                accent={sre.critical_incidents ? "error" : undefined}
              />
              <Stat label="Apps tracked" value={fmtNum(sre.total_apps)} />
              <Stat label="Cost (24h)" value={fmtUsd(sre.latest_daily_cost)} />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

// ── Small presentational helpers ──────────────────────────────────────

function Kpi({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: "error" | "ok";
}) {
  const color = accent === "error" ? "var(--error-ink)" : accent === "ok" ? "var(--ok-ink)" : "var(--ink-strong)";
  return (
    <div className="panel" style={{ padding: 14 }}>
      <div className="label-eyebrow">{label}</div>
      <div className="font-mono tabular-nums" style={{ fontSize: 26, fontWeight: 600, color, marginTop: 4 }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 11, color: "var(--ink-muted)", marginTop: 2 }}>{sub}</div>
      )}
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: "error" }) {
  return (
    <div className="rounded" style={{ background: "var(--surface-muted)", padding: 12 }}>
      <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>{label}</div>
      <div
        className="font-mono tabular-nums"
        style={{ fontSize: 20, fontWeight: 600, color: accent === "error" ? "var(--error-ink)" : "var(--ink)" }}
      >
        {value}
      </div>
    </div>
  );
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return <th style={{ padding: "8px 16px", textAlign: right ? "right" : "left" }}>{children}</th>;
}
function Td({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <td
      className="font-mono tabular-nums"
      style={{ padding: "8px 16px", textAlign: "right", color: "var(--ink-soft)", ...style }}
    >
      {children}
    </td>
  );
}

function MemoryPanel({
  mem,
  days,
  onChanged,
}: {
  mem: MemoryAnalytics | null;
  days: number;
  onChanged: () => void;
}) {
  const t = mem?.totals ?? {};
  const healthOk = !!mem?.health?.ok;
  const embeddedPct = t.total ? Math.round(((t.embedded ?? 0) / t.total) * 100) : null;
  const fmtAge = (epoch: number) => {
    const d = (Date.now() / 1000 - epoch) / 86400;
    if (d < 1 / 24) return "just now";
    if (d < 1) return `${Math.round(d * 24)}h ago`;
    return `${Math.round(d)}d ago`;
  };
  const forget = async (id: string) => {
    if (!window.confirm("Forget this memory? Future runs will no longer recall it.")) return;
    try {
      await api.analytics.memoryForget(id);
      onChanged();
    } catch {
      // panel reload on next refresh — deletion errors are non-fatal here
    }
  };
  return (
    <div className="panel" style={{ padding: 16 }}>
      <div className="flex items-center justify-between mb-3">
        <div className="label-eyebrow flex items-center gap-2">
          <Brain className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} /> Agentic memory
        </div>
        <span className={`pill ${healthOk ? "pill-ok" : "pill-warn"}`}>
          {mem ? `${mem.provider}${healthOk ? "" : " — degraded"}` : "unavailable"}
        </span>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
        <Stat label="Memories" value={fmtNum(t.total)} />
        <Stat label="Embedded" value={embeddedPct == null ? "—" : `${embeddedPct}%`} />
        <Stat label="Episodic" value={fmtNum(t.episodic)} />
        <Stat label="Semantic" value={fmtNum(t.semantic)} />
        <Stat label="Procedural" value={fmtNum(t.procedural)} />
        <Stat label="Recalls served" value={fmtNum(t.total_recalls)} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Writes over time */}
        <div>
          <div className="label-eyebrow mb-2">Memories written ({days}d)</div>
          <LineChart
            height={160}
            series={[
              {
                name: "Episodic",
                color: "var(--info)",
                points: (mem?.timeseries ?? []).map((p) => ({ label: p.date, value: p.episodic })),
              },
              {
                name: "Semantic",
                color: "var(--ok)",
                points: (mem?.timeseries ?? []).map((p) => ({ label: p.date, value: p.semantic })),
              },
              {
                name: "Procedural",
                color: "var(--accent)",
                points: (mem?.timeseries ?? []).map((p) => ({ label: p.date, value: p.procedural })),
              },
            ]}
          />
          <div className="label-eyebrow mt-4 mb-2">By agent</div>
          <HBarChart
            color="var(--accent)"
            rows={(mem?.by_agent ?? []).slice(0, 6).map((a) => ({ label: a.agent || "(unscoped)", value: a.memories }))}
            formatValue={(n) => fmtNum(n)}
          />
        </div>

        {/* Recent memories */}
        <div>
          <div className="label-eyebrow mb-2">Latest memories</div>
          {(mem?.recent ?? []).length === 0 && (
            <div style={{ color: "var(--ink-muted)", fontSize: 12 }}>
              Nothing written yet — memories appear after SRE sweeps, finished ALM runs, and repo scans.
            </div>
          )}
          <div className="flex flex-col gap-2" style={{ maxHeight: 320, overflowY: "auto" }}>
            {(mem?.recent ?? []).map((r, i) => (
              <div key={i} className="rounded" style={{ background: "var(--surface-muted)", padding: 10 }}>
                <div className="flex items-center gap-2 mb-1" style={{ fontSize: 11 }}>
                  <span className="pill">{r.type}</span>
                  <span style={{ color: "var(--ink)" }}>{r.agent || "—"}</span>
                  <span className="truncate" style={{ color: "var(--ink-muted)" }} title={r.repo}>
                    {r.repo}
                  </span>
                  <span style={{ marginLeft: "auto", color: "var(--ink-muted)", whiteSpace: "nowrap" }}>
                    {fmtAge(r.created_at)}
                  </span>
                  <button
                    onClick={() => forget(r.provider_id)}
                    title="Forget this memory"
                    aria-label="Forget this memory"
                    style={{
                      color: "var(--ink-muted)",
                      fontSize: 11,
                      padding: "0 2px",
                      lineHeight: 1,
                      cursor: "pointer",
                    }}
                  >
                    ✕
                  </button>
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-soft)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {r.content}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function TelemetryPanel({ tel }: { tel: TelemetryHealth | null }) {
  const exporting = !!tel?.telemetry?.exporting;
  const provider = tel?.telemetry?.provider ?? tel?.provider ?? "noop";
  const promOk = !!tel?.prometheus?.reachable;
  return (
    <div className="panel" style={{ padding: 16 }}>
      <div className="flex items-center justify-between mb-3">
        <div className="label-eyebrow flex items-center gap-2">
          <Radio className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} /> Telemetry &amp; OTel collector
        </div>
        <span className={`pill ${exporting ? "pill-ok" : "pill-warn"}`}>
          {exporting ? "Exporting" : "Telemetry off"}
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <HealthChip
          ok={exporting}
          icon={exporting ? <CheckCircle2 className="w-4 h-4" /> : <Activity className="w-4 h-4" />}
          label="OTLP export"
          detail={exporting ? "active" : "disabled"}
        />
        <HealthChip ok label="Provider" detail={provider} neutral />
        <HealthChip
          ok={promOk}
          icon={promOk ? <CheckCircle2 className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
          label="Prometheus"
          detail={promOk ? "reachable" : "unreachable"}
        />
        <HealthChip ok label="Endpoint" detail={tel?.telemetry?.endpoint || "—"} neutral mono />
      </div>
      {!exporting && (
        <p className="text-xs mt-3" style={{ color: "var(--ink-muted)" }}>
          Set <code>DEVAI_TELEMETRY_PROVIDER=otel</code> and <code>DEVAI_OTEL_ENDPOINT</code> to the collector
          (OTLP/HTTP :4318) to start exporting traces + metrics.
        </p>
      )}
    </div>
  );
}

function HealthChip({
  ok,
  label,
  detail,
  icon,
  neutral,
  mono,
}: {
  ok: boolean;
  label: string;
  detail: string;
  icon?: React.ReactNode;
  neutral?: boolean;
  mono?: boolean;
}) {
  const color = neutral ? "var(--ink-soft)" : ok ? "var(--ok-ink)" : "var(--warn-ink)";
  return (
    <div className="rounded flex items-start gap-2" style={{ background: "var(--surface-muted)", padding: 12 }}>
      {icon && <span style={{ color }}>{icon}</span>}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>{label}</div>
        <div
          className={mono ? "font-mono" : ""}
          style={{ fontSize: 13, color, fontWeight: 600, wordBreak: "break-all" }}
          title={detail}
        >
          {detail}
        </div>
      </div>
    </div>
  );
}
