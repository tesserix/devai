"use client";

/**
 * Per-run detail (DASH-7, DASH-10, DASH-12, DASH-13, DASH-2).
 *
 * Replaces the opaque text status with the canonical run DAG: the run's live
 * state is overlaid onto its blueprint graph (colour-coded stages, gate markers
 * with inline approve/reject, per-stage durations). The supervisor's advisory
 * delegation plan is surfaced when present. The PREVIEW tab gates the iframe
 * behind the shared BuildingState until the environment is actually up, for both
 * the pipeline-provisioned and user-started entry paths, and sandboxes the
 * iframe so it is never same-origin-with-scripts.
 *
 * Tabs:
 *   OVERVIEW   — the live run DAG + delegation plan + checkpoints + inject.
 *   TIMELINE   — A2A messages between agents (api.getRunA2A).
 *   PREVIEW    — gated live iframe (BuildingState → iframe), per-container health.
 *   REPO       — read-only file tree + viewer (RepoPanel, SSE).
 *   CHAT       — conversational interface scoped to this run.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  api,
  type DelegationPlan,
  type PipelineRun,
  type PreviewDiagnosis,
  type PreviewSession,
} from "@/lib/api";
import type { A2AMessage } from "@/lib/api";
import { A2AFeed } from "@/components/a2a-feed";
import { ChatPanel } from "@/components/chat-panel";
import { LiveLogs } from "@/components/live-logs";
import { RepoPanel } from "@/components/repo-panel";
import { RunDAG } from "@/components/pipeline-flow";
import { CheckpointTimeline, type Checkpoint } from "@/components/checkpoint-timeline";
import { RunStateBadge, normalizeRunState } from "@/components/run-state-badge";
import { GuidancePanel, HelpPopover } from "@/components/guidance";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { BuildingState, type BuildPhase } from "@/components/building-state";
import { EmptyState } from "@/components/empty-state";
import { useToast } from "@/components/toast";
import {
  AlertTriangle,
  ArrowLeft,
  ExternalLink,
  GitBranch,
  Layers,
  Send,
  ShieldQuestion,
  Workflow,
} from "lucide-react";

type Tab = "overview" | "logs" | "timeline" | "preview" | "repo" | "chat";

interface RunDetail extends PipelineRun {
  blueprint?: string;
  intent?: string;
  triggered_by?: string;
  trigger_type?: string;
  source?: string;
  trace_id?: string;
  current_stage?: string;
  state?: string;
  error?: string;
  failed_stage?: string;
  stages_completed?: string[];
  stages_skipped?: string[];
  stages_failed?: string[];
  stage_events?: Array<{ stage: string; phase?: string; duration_ms?: number }>;
  checkpoints?: Array<{ sha: string; label?: string; stage?: string; ts?: number }>;
  agent_context?: Record<string, unknown> & {
    preview_url?: string;
    editor_url?: string;
    delegation_plan?: DelegationPlan;
    injections?: Array<{ message: string; ts: number; source?: string }>;
  };
}

// Gate stages whose inline approve/reject should fire. Built from the
// approvals endpoint (pending only) keyed by gate name.
type Gate = { gate: string; agent?: string; timestamp?: number };

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const runId = String(params?.id ?? "");
  const toast = useToast();

  const [tab, setTab] = useState<Tab>("overview");
  const [run, setRun] = useState<RunDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [gates, setGates] = useState<Gate[]>([]);
  const [a2a, setA2a] = useState<A2AMessage[]>([]);
  const [delegation, setDelegation] = useState<DelegationPlan | null>(null);

  // Poll the run + its gates. SSE could replace this; the 4s poll matches the
  // rest of the dashboard and keeps the DAG filling in without a refresh.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    const refresh = async () => {
      try {
        const data = (await api.getRun(runId)) as RunDetail;
        if (!cancelled) {
          setRun(data);
          setNotFound(false);
        }
      } catch (e) {
        // 404 (unknown run) is terminal; transient errors just keep last data.
        const msg = String((e as Error)?.message ?? e);
        if (!cancelled && msg.includes("404")) setNotFound(true);
      }
      try {
        const g = await api.getApprovals(runId);
        if (!cancelled) setGates(Array.isArray(g) ? g : []);
      } catch {
        /* approvals endpoint optional — non-fatal */
      }
    };

    refresh();
    const id = window.setInterval(refresh, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [runId]);

  // A2A timeline (DASH-7) — via the api method, not a hardcoded /dashboard fetch.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const load = () =>
      api
        .getRunA2A(runId)
        .then((m) => !cancelled && setA2a(Array.isArray(m) ? m : []))
        .catch(() => undefined);
    load();
    const id = window.setInterval(load, 6000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [runId]);

  // Advisory supervisor delegation plan (DASH-12). Prefer the dedicated
  // endpoint; fall back to whatever the run carries on its handover bag.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    api
      .getDelegationPlan(runId)
      .then((p) => !cancelled && p && setDelegation(p))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const blueprint = run?.blueprint || "alm-pipeline";
  const ctx: NonNullable<RunDetail["agent_context"]> = run?.agent_context ?? {};
  const previewUrl = (ctx.preview_url as string) || "";
  const effectiveDelegation =
    delegation ?? (ctx.delegation_plan as DelegationPlan | undefined) ?? null;
  const injections = ctx.injections ?? [];

  const checkpoints: Checkpoint[] = useMemo(
    () =>
      (run?.checkpoints ?? []).map((cp) => ({
        sha: cp.sha,
        label: cp.label || cp.stage || "checkpoint",
        stage: cp.stage,
        ts: cp.ts,
      })),
    [run?.checkpoints],
  );

  // Pending gates → name set so RunDAG can flip them to gate-pending.
  const pendingGates = useMemo(() => {
    const m: Record<string, boolean> = {};
    for (const g of gates) m[g.gate] = true;
    return m;
  }, [gates]);

  const norm = normalizeRunState(run?.state || run?.stage);
  const runActive = norm === "running" || norm === "paused" || norm === "queued" || norm === "gate-pending";

  // DASH-10 honesty: injections are only consumed by a re-planning stage
  // (Supervisor or a crew lead). If neither is in the blueprint, or the run is
  // no longer active, the control is disabled with an explanation rather than
  // silently no-opping.
  const [blueprintHasReplanner, setBlueprintHasReplanner] = useState<boolean | null>(null);
  useEffect(() => {
    if (!blueprint) return;
    let cancelled = false;
    api
      .getBlueprintGraph(blueprint)
      .then((g) => {
        if (cancelled) return;
        const has = g.nodes.some((n) => {
          const a = (n.agent || "").toLowerCase();
          const t = (n.type || "").toLowerCase();
          return a.includes("supervisor") || a.includes("crew") || t.includes("crew") || t.includes("supervisor");
        });
        setBlueprintHasReplanner(has);
      })
      .catch(() => !cancelled && setBlueprintHasReplanner(null));
    return () => {
      cancelled = true;
    };
  }, [blueprint]);

  const injectReason = !runActive
    ? "Injecting only works while a run is active."
    : blueprintHasReplanner === false
      ? "This blueprint has no Supervisor or crew stage to read an injected instruction, so it would have no effect."
      : undefined;

  async function approveGate(gate: string) {
    try {
      await api.approveGate(runId, gate);
      setGates((prev) => prev.filter((g) => g.gate !== gate));
      toast.success(`Approved gate "${gate}"`);
    } catch (e) {
      toast.error(String((e as Error)?.message ?? e));
    }
  }
  async function rejectGate(gate: string) {
    try {
      await api.rejectGate(runId, gate);
      setGates((prev) => prev.filter((g) => g.gate !== gate));
      toast.info(`Rejected gate "${gate}" — run will stop`);
    } catch (e) {
      toast.error(String((e as Error)?.message ?? e));
    }
  }

  async function inject(message: string) {
    await api.injectMessage(runId, message);
  }

  if (notFound) {
    return (
      <div className="p-7">
        <Breadcrumbs items={[{ label: "Runs", href: "/runs" }, { label: runId }]} className="mb-6" />
        <EmptyState
          Icon={AlertTriangle}
          title="Run not found"
          description={`No run with id "${runId}" — it may have expired from memory or never existed.`}
          action={{ label: "Back to runs", href: "/runs" }}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0" style={{ background: "var(--canvas)" }}>
      {/* Header */}
      <div
        className="px-6 pt-4 pb-3 shrink-0"
        style={{ background: "var(--surface)", borderBottom: "1px solid var(--border-subtle)" }}
      >
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-[13px] font-medium mb-2 transition-colors hover:underline underline-offset-2"
          style={{ color: "var(--ink-muted)" }}
        >
          <ArrowLeft className="w-3.5 h-3.5" /> Back to Fleet
        </Link>
        <Breadcrumbs
          items={[
            { label: "Fleet", href: "/" },
            { label: "Runs", href: "/runs" },
            { label: run?.repo || runId },
          ]}
          className="mb-2.5"
        />
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-2.5 flex-wrap">
              <h1 className="text-base font-semibold truncate" style={{ color: "var(--ink-strong)" }}>
                {run?.repo || "—"}
              </h1>
              <RunStateBadge state={run?.state || run?.stage || "pending"} />
            </div>
            <div className="mt-1 flex items-center gap-2.5 flex-wrap text-[12px]" style={{ color: "var(--ink-muted)" }}>
              <span className="font-mono">{runId}</span>
              <span aria-hidden>·</span>
              <span className="inline-flex items-center gap-1.5">
                <Layers className="w-3 h-3" />
                <span className="font-mono">{blueprint}</span>
                <HelpPopover term="blueprint" />
              </span>
              {run?.triggered_by && (
                <>
                  <span aria-hidden>·</span>
                  <span>
                    by <code>{run.triggered_by}</code>
                  </span>
                </>
              )}
              {(run?.source || run?.trigger_type) && (
                <>
                  <span aria-hidden>·</span>
                  <span>{run.source || run.trigger_type}</span>
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Tab bar */}
      <div
        className="px-6 shrink-0 flex items-center gap-0"
        style={{ background: "var(--surface)", borderBottom: "1px solid var(--border-subtle)" }}
      >
        {(
          [
            { key: "overview", label: "Overview" },
            { key: "logs", label: "Logs" },
            { key: "timeline", label: "Timeline" },
            { key: "preview", label: "Preview" },
            { key: "repo", label: "Repo" },
            { key: "chat", label: "Chat" },
          ] as const
        ).map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className="px-3.5 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors"
              style={{
                borderColor: active ? "var(--accent)" : "transparent",
                color: active ? "var(--ink-strong)" : "var(--ink-muted)",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "overview" && (
          <div className="h-full overflow-y-auto p-6">
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px] gap-6 max-w-[1400px]">
              <div className="min-w-0 space-y-5">
                <GuidancePanel id="run-detail" />

                {/* Live run DAG */}
                <section
                  className="rounded-lg border p-4"
                  style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
                >
                  <div className="flex items-center gap-1.5 mb-3">
                    <Workflow className="w-3.5 h-3.5" style={{ color: "var(--ink-muted)" }} />
                    <span className="label-eyebrow">Stages</span>
                    <HelpPopover term="stage" />
                  </div>
                  <RunDAG
                    blueprint={blueprint}
                    run={run}
                    pendingGates={pendingGates}
                    onApprove={approveGate}
                    onReject={rejectGate}
                  />
                </section>

                {/* Failure callout */}
                {norm === "failed" && (run?.error || run?.failed_stage) && (
                  <section
                    className="rounded-lg border p-4 flex gap-3"
                    style={{ background: "var(--error-soft-bg)", borderColor: "var(--error-soft-bd)" }}
                  >
                    <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" style={{ color: "var(--error)" }} />
                    <div className="min-w-0">
                      <h3 className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                        Failed{run?.failed_stage ? ` at ${run.failed_stage.replace(/_/g, " ")}` : ""}
                      </h3>
                      {run?.error && (
                        <p className="mt-1 text-[13px] leading-relaxed font-mono break-words" style={{ color: "var(--error-ink)" }}>
                          {run.error}
                        </p>
                      )}
                    </div>
                  </section>
                )}

                <DelegationCard plan={effectiveDelegation} />
              </div>

              {/* Right rail */}
              <div className="space-y-5">
                <CheckpointTimeline
                  checkpoints={checkpoints}
                  rollbackDisabledReason="Roll back from the run's working tree once a checkpoint is selected — available while the runner pod is live."
                />
                <InjectCard onInject={inject} disabledReason={injectReason} injections={injections} />
              </div>
            </div>
          </div>
        )}

        {tab === "logs" && (
          <div className="h-full overflow-y-auto p-6">
            <div className="max-w-5xl">
              <p className="mb-3 text-[13px]" style={{ color: "var(--ink-soft)" }}>
                Live per-stage stream as the crew works — each stage logs as it starts, runs, and
                completes. Watch the code and docs get built here; the developed files land in the{" "}
                <button
                  type="button"
                  onClick={() => setTab("repo")}
                  className="font-medium hover:underline"
                  style={{ color: "var(--accent)" }}
                >
                  Repo
                </button>{" "}
                tab and the agents&apos; reasoning in{" "}
                <button
                  type="button"
                  onClick={() => setTab("timeline")}
                  className="font-medium hover:underline"
                  style={{ color: "var(--accent)" }}
                >
                  Timeline
                </button>
                .
              </p>
              <LiveLogs taskId={runId} />
            </div>
          </div>
        )}

        {tab === "timeline" && (
          <div className="h-full overflow-y-auto p-6">
            <div className="max-w-3xl">
              <div className="flex items-center gap-1.5 mb-3">
                <span className="label-eyebrow">Agent-to-agent messages ({a2a.length})</span>
                <HelpPopover term="handover" />
              </div>
              {a2a.length === 0 ? (
                <EmptyState
                  title="No agent messages yet"
                  description="As stages run, the agents' request / response / handoff messages stream in here."
                />
              ) : (
                <A2AFeed messages={a2a} />
              )}
            </div>
          </div>
        )}

        {tab === "preview" && (
          <PreviewTab runId={runId} repo={run?.repo} pipelineUrl={previewUrl} />
        )}

        {tab === "repo" && <RepoPanel runId={runId} />}

        {tab === "chat" && (
          <div className="h-full">
            <ChatPanel runId={runId} repo={run?.repo} stage={run?.current_stage || run?.stage} />
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Delegation plan (advisory) — DASH-12
// ─────────────────────────────────────────────────────────────────────────

function DelegationCard({ plan }: { plan: DelegationPlan | null }) {
  if (!plan) return null;
  const assignments = Array.isArray(plan.assignments) ? plan.assignments : [];
  if (!plan.summary && assignments.length === 0 && !plan.tracking_issue_url) return null;

  return (
    <section
      className="rounded-lg border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
    >
      <div className="flex items-center gap-1.5 mb-1">
        <ShieldQuestion className="w-3.5 h-3.5" style={{ color: "var(--ink-muted)" }} />
        <span className="label-eyebrow">Supervisor delegation plan</span>
        <HelpPopover term="delegation-plan" />
      </div>
      <p className="text-[12px] leading-relaxed mb-3" style={{ color: "var(--ink-muted)" }}>
        Advisory only — the executed stages remain the fixed blueprint DAG. This is the supervisor&apos;s reasoning, surfaced so you can read how it would split the work.
      </p>
      {plan.summary && (
        <p className="text-[13px] leading-relaxed mb-3" style={{ color: "var(--ink-soft)" }}>
          {plan.summary}
        </p>
      )}
      {assignments.length > 0 && (
        <ul className="space-y-2">
          {assignments.map((a, i) => (
            <li
              key={i}
              className="rounded-md border p-2.5"
              style={{ background: "var(--surface-muted)", borderColor: "var(--border-subtle)" }}
            >
              <div className="flex items-center gap-2 flex-wrap text-[12px]">
                {a.stage && (
                  <span className="font-mono font-medium" style={{ color: "var(--ink-strong)" }}>
                    {a.stage}
                  </span>
                )}
                {a.agent && (
                  <span className="pill pill-muted !text-[10px]">{a.agent}</span>
                )}
              </div>
              {a.task && (
                <p className="mt-1 text-[12.5px] leading-snug" style={{ color: "var(--ink-soft)" }}>
                  {a.task}
                </p>
              )}
              {a.rationale && (
                <p className="mt-1 text-[11.5px] leading-snug italic" style={{ color: "var(--ink-muted)" }}>
                  {a.rationale}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {plan.tracking_issue_url && (
        <a
          href={plan.tracking_issue_url}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex items-center gap-1.5 text-[12px] font-medium underline-offset-2 hover:underline"
          style={{ color: "var(--accent)" }}
        >
          <GitBranch className="w-3 h-3" />
          Tracking issue
          <ExternalLink className="w-3 h-3" />
        </a>
      )}
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Inject instruction — DASH-10 (honest: disabled with reason if no consumer)
// ─────────────────────────────────────────────────────────────────────────

function InjectCard({
  onInject,
  disabledReason,
  injections,
}: {
  onInject: (message: string) => Promise<void>;
  disabledReason?: string;
  injections: Array<{ message: string; ts: number; source?: string }>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const disabled = !!disabledReason;

  async function submit() {
    const msg = value.trim();
    if (!msg || disabled || busy) return;
    setBusy(true);
    setErr("");
    try {
      await onInject(msg);
      setValue("");
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="rounded-lg border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
    >
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className="label-eyebrow">Inject instruction</span>
      </div>
      <p className="text-[12px] leading-relaxed mb-2.5" style={{ color: "var(--ink-muted)" }}>
        Add an instruction for the next re-planning stage (Supervisor or crew lead) to read.
      </p>

      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={disabled || busy}
        rows={3}
        placeholder={disabled ? "Unavailable for this run" : "e.g. also add rate limiting to the new endpoint"}
        className="field w-full resize-none"
        style={disabled ? { opacity: 0.6, cursor: "not-allowed" } : undefined}
        aria-disabled={disabled}
        title={disabledReason}
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[11px]" style={{ color: "var(--ink-muted)" }} title={disabledReason}>
          {disabledReason || "Best-effort — applied at the next planning stage."}
        </span>
        <button
          type="button"
          onClick={submit}
          disabled={disabled || busy || !value.trim()}
          className="btn-primary !py-1.5 !text-sm"
          title={disabledReason}
        >
          <Send className="w-3.5 h-3.5" />
          {busy ? "Sending…" : "Inject"}
        </button>
      </div>
      {err && (
        <p className="mt-2 text-[12px]" style={{ color: "var(--error-ink)" }}>
          {err}
        </p>
      )}

      {injections.length > 0 && (
        <div className="mt-3 pt-3 border-t space-y-1.5" style={{ borderColor: "var(--border-subtle)" }}>
          <div className="label-eyebrow">Sent ({injections.length})</div>
          {injections.slice(-4).map((inj, i) => (
            <p key={i} className="text-[12px] leading-snug" style={{ color: "var(--ink-soft)" }}>
              {inj.message}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Preview tab — DASH-13 + DASH-2
// ─────────────────────────────────────────────────────────────────────────

// Map a preview status + diagnoses to a coarse bring-up phase for BuildingState.
// We can't see clone/install/boot directly, so we infer: no containers visible
// yet → clone; an install failure → install; containers reported (pending or
// otherwise) → boot.
function derivePhase(session: PreviewSession | null, diagnoses: PreviewDiagnosis[]): BuildPhase {
  if (diagnoses.some((d) => d.class === "install_failed")) return "install";
  if (diagnoses.length === 0) return session ? "install" : "clone";
  return "boot";
}

function PreviewTab({
  runId,
  repo,
  pipelineUrl,
}: {
  runId: string;
  repo?: string;
  pipelineUrl: string;
}) {
  // The pipeline-provisioned URL (spin_preview_pod) takes precedence. Otherwise
  // the user can start an on-demand preview for this run's repo. Both resolve to
  // the same forwarded host and flow through the SAME gated BuildingState.
  const [session, setSession] = useState<PreviewSession | null>(null);
  const [diagnoses, setDiagnoses] = useState<PreviewDiagnosis[]>([]);
  const [starting, setStarting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [err, setErr] = useState("");
  // For the pipeline path we have a URL but no session id — treat it as a
  // ready preview gated only by an iframe load probe is impractical, so we
  // optimistically show it but still expose Stop/Open affordances.
  const pollRef = useRef<number | null>(null);

  const liveUrl = session?.preview_url || session?.fe_url || pipelineUrl;
  const sessionId = session?.session_id || "";
  const status = session?.status;
  const isReady = status === "running" || (!session && !!pipelineUrl);

  // Verify (no heal) loop for per-container health while not yet ready.
  const refreshHealth = useCallback(async () => {
    if (!sessionId) return;
    setChecking(true);
    try {
      const r = await api.preview.verify(sessionId, false);
      setDiagnoses(r.diagnoses ?? []);
      // Reconcile session status from the report when it disagrees.
      setSession((prev) =>
        prev
          ? {
              ...prev,
              status:
                r.status === "healthy"
                  ? "running"
                  : r.status === "error"
                    ? "failed"
                    : r.status === "degraded"
                      ? "degraded"
                      : "starting",
            }
          : prev,
      );
    } catch {
      /* transient — keep last */
    } finally {
      setChecking(false);
    }
  }, [sessionId]);

  // Refresh the session record itself (status/url) and its health.
  const refreshSession = useCallback(async () => {
    if (!sessionId) return;
    try {
      const s = await api.preview.get(sessionId);
      setSession(s);
    } catch {
      /* keep last */
    }
    await refreshHealth();
  }, [sessionId, refreshHealth]);

  // Auto-poll while a session is starting/degraded; stop once running/failed.
  useEffect(() => {
    if (!sessionId) return;
    if (status === "running" || status === "failed" || status === "stopped") {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    refreshSession();
    pollRef.current = window.setInterval(refreshSession, 5000);
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [sessionId, status, refreshSession]);

  async function start(ref = "main") {
    if (!repo) return;
    setStarting(true);
    setErr("");
    try {
      const s = await api.preview.start(repo, ref);
      setSession(s);
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      setStarting(false);
    }
  }

  async function stop() {
    if (!sessionId) {
      // Pipeline-provisioned preview — no session handle to stop from here.
      setSession(null);
      return;
    }
    try {
      await api.preview.stop(sessionId);
      setSession((prev) => (prev ? { ...prev, status: "stopped" } : prev));
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    }
  }

  // No preview yet — offer to start one (or explain it isn't available).
  if (!liveUrl) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="max-w-md w-full">
          <GuidancePanel id="preview" className="mb-5" />
          <EmptyState
            title={repo ? "No live preview yet" : "No preview available"}
            description={
              repo
                ? "Spin up an ephemeral environment for this repo. It clones, installs and boots a dev server — which can take a few minutes."
                : "This run has no repository to preview."
            }
            action={
              repo
                ? { label: starting ? "Starting…" : "Start preview", onClick: () => start() }
                : undefined
            }
          />
          {err && (
            <p className="mt-3 text-center text-[13px]" style={{ color: "var(--error-ink)" }}>
              {err}
            </p>
          )}
        </div>
      </div>
    );
  }

  // Building / failed / degraded → gate behind the shared BuildingState.
  if (!isReady) {
    const phase = derivePhase(session, diagnoses);
    return (
      <div className="h-full">
        <BuildingState
          session={{
            repo: repo || session?.repo || "",
            ref: session?.ref || "main",
            status: (status as PreviewSession["status"]) || "starting",
            session_id: sessionId || undefined,
          }}
          phase={phase}
          diagnoses={diagnoses}
          onRefresh={sessionId ? refreshHealth : undefined}
          refreshing={checking}
          onStop={stop}
          note={
            err ||
            (status === "failed"
              ? "The preview failed to come up. Check the failing container below, then start a new one."
              : undefined)
          }
        />
      </div>
    );
  }

  // Ready → toolbar + sandboxed iframe (DASH-2).
  return (
    <div className="h-full flex flex-col">
      <div
        className="px-4 py-2 flex items-center gap-3 text-xs shrink-0"
        style={{ borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-muted)" }}
      >
        <span style={{ color: "var(--ink-muted)" }}>preview</span>
        <a
          href={liveUrl}
          target="_blank"
          rel="noreferrer"
          className="font-mono truncate underline-offset-2 hover:underline"
          style={{ color: "var(--accent)" }}
        >
          {liveUrl}
        </a>
        {diagnoses.length > 0 && <HealthDots diagnoses={diagnoses} />}
        <div className="ml-auto flex items-center gap-2 shrink-0">
          <a
            href={liveUrl}
            target="_blank"
            rel="noreferrer"
            className="btn-secondary !py-1 !px-2 !text-xs"
          >
            <ExternalLink className="w-3 h-3" />
            Open
          </a>
          <button type="button" onClick={stop} className="btn-ghost !py-1 !px-2 !text-xs">
            Stop
          </button>
        </div>
      </div>
      {/*
        DASH-2: the preview renders untrusted repo code. The sandbox MUST NOT
        combine allow-scripts WITH allow-same-origin (that lets the framed app
        script its way out of the sandbox via same-origin access to this
        document). The preview is served from a distinct *.tesserix.app host, so
        dropping allow-same-origin keeps it functional while sandboxed. We also
        send no referrer.
      */}
      <iframe
        src={liveUrl}
        className="flex-1 w-full"
        style={{ border: "none", background: "white" }}
        sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"
        referrerPolicy="no-referrer"
        title={`Live preview of ${repo || "the run"}`}
      />
    </div>
  );
}

const HEALTH_DOT: Record<PreviewDiagnosis["class"], string> = {
  healthy: "dot-ok",
  pending: "dot-running",
  oom: "dot-error",
  crashloop: "dot-error",
  install_failed: "dot-error",
  image_pull: "dot-error",
  port_mismatch: "dot-warn",
  cors: "dot-warn",
  unknown: "dot-muted",
};

function HealthDots({ diagnoses }: { diagnoses: PreviewDiagnosis[] }) {
  return (
    <div className="flex items-center gap-2.5">
      {diagnoses.map((d, i) => (
        <span
          key={`${d.container}-${i}`}
          className="inline-flex items-center gap-1"
          title={`${d.container}: ${d.class}${d.detail ? ` — ${d.detail}` : ""}`}
          style={{ color: "var(--ink-muted)" }}
        >
          <span className={`dot ${HEALTH_DOT[d.class]}`} aria-hidden />
          <span className="font-mono">{d.container}</span>
        </span>
      ))}
    </div>
  );
}
