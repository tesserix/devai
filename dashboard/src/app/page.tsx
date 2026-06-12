"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type PipelineRun } from "@/lib/api";
import { useRunEvents } from "@/lib/use-run-events";
import { AGENT_INFO } from "@/lib/constants";
import { PipelineFlow } from "@/components/pipeline-flow";
import { AgentCard } from "@/components/agent-card";
import { AgentHierarchy } from "@/components/agent-hierarchy";
import { A2AFeed } from "@/components/a2a-feed";
import { RunList } from "@/components/run-list";
import { TriggerDialog } from "@/components/trigger-dialog";
import { ApprovalBanner } from "@/components/approval-banner";
import { ChatPanel } from "@/components/chat-panel";
import { useToast } from "@/components/toast";

type Tab = "overview" | "hierarchy" | "agents" | "a2a" | "events" | "chat" | "config";

export default function DashboardPage() {
  const toast = useToast();
  const [runs, setRuns] = useState<PipelineRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [selectedRun, setSelectedRun] = useState<PipelineRun | null>(null);
  const [approvals, setApprovals] = useState<Awaited<ReturnType<typeof api.getApprovals>>>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [triggerOpen, setTriggerOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchRuns = useCallback(async () => {
    try {
      const data = await api.listRuns();
      setRuns(data);
      if (!selectedRunId && data.length > 0) {
        setSelectedRunId(data[0].run_id);
      }
    } catch {
      // API may not be available
    } finally {
      setLoading(false);
    }
  }, [selectedRunId]);

  useEffect(() => {
    fetchRuns();
    const interval = setInterval(fetchRuns, 5000);
    return () => clearInterval(interval);
  }, [fetchRuns]);

  const refreshDetail = useCallback(async () => {
    if (!selectedRunId) return;
    try {
      const run = await api.getRun(selectedRunId);
      setSelectedRun(run);
      const pendingApprovals = await api.getApprovals(selectedRunId);
      setApprovals(pendingApprovals);
    } catch {
      // Run may not exist
    }
  }, [selectedRunId]);

  // Live layer: one SSE subscription per selected run. The backend hub
  // mutates the task (agents / a2a / routing) BEFORE emitting each
  // envelope, so a debounced snapshot re-fetch right after any envelope
  // gives every tab a consistent real-time view with zero client-side
  // state merging. Polling below stays as the reconciliation fallback.
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { connected: live } = useRunEvents(
    selectedRunId ?? null,
    useCallback(() => {
      if (refreshTimer.current) return; // trailing debounce — one fetch per burst
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null;
        void refreshDetail();
      }, 400);
    }, [refreshDetail]),
  );

  useEffect(() => {
    if (!selectedRunId) return;
    refreshDetail();
    // SSE connected → slow reconciliation poll; disconnected → 3s fallback.
    const interval = setInterval(refreshDetail, live ? 15000 : 3000);
    return () => {
      clearInterval(interval);
      if (refreshTimer.current) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
    };
  }, [selectedRunId, refreshDetail, live]);

  const handleTrigger = async (
    repo: string,
    requirements: string,
    opts?: { blueprint?: string; autonomy?: string; brainstorm?: boolean },
  ) => {
    // Forward the blueprint picked in the dialog (DASH-6) so the backend no
    // longer silently falls back to the default blueprint.
    const result = await api.triggerPipeline(repo, requirements, opts);
    setSelectedRunId(result.run_id);
    await fetchRuns();
  };

  const handleRetrigger = async (runId: string) => {
    // Re-uses the ORIGINAL requirements from the failed run server-side.
    // Previously this hardcoded the literal string "Retry failed pipeline
    // run" as the requirements, which made the dev agent build a
    // pipeline-retry feature in the target repo instead of replaying
    // whatever the user actually asked for.
    const result = await api.retriggerRun(runId);
    setSelectedRunId(result.run_id);
    await fetchRuns();
  };

  // Run controls. Errors used to be swallowed silently (the button just did
  // nothing) — surface them as a toast so a failed pause/stop is visible.
  const runControl = async (
    runId: string,
    verb: string,
    fn: (id: string) => Promise<unknown>,
  ) => {
    try {
      await fn(runId);
      toast.success(`Run ${runId.slice(0, 8)} ${verb}.`);
    } catch (e) {
      toast.error(`Couldn't ${verb.replace(/ed$/, "")} run: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      await fetchRuns();
    }
  };

  const handlePause = (runId: string) => runControl(runId, "paused", api.pauseRun);
  const handleResume = (runId: string) => runControl(runId, "resumed", api.resumeRun);
  const handleStop = (runId: string) => runControl(runId, "stopped", api.stopRun);
  // Continue a failed run from its failed stage (vs Retry = full new run).
  const handleResumeFailed = (runId: string) =>
    runControl(runId, "continued from the failed stage", async (id) => {
      await api.resumeFailedRun(id);
      setSelectedRunId(id);
    });

  const handleDelete = async (runId: string) => {
    await runControl(runId, "deleted", api.deleteRun);
    // If the deleted run was selected, clear the selection so the detail pane
    // doesn't keep polling a now-gone run.
    if (selectedRunId === runId) setSelectedRunId(undefined);
  };

  const handleApprove = async (gate: string) => {
    if (!selectedRunId) return;
    await api.approveGate(selectedRunId, gate);
    setApprovals((prev) => prev.filter((a) => a.gate !== gate));
  };

  const handleReject = async (gate: string) => {
    if (!selectedRunId) return;
    await api.rejectGate(selectedRunId, gate);
    setApprovals((prev) => prev.filter((a) => a.gate !== gate));
  };

  const runContext = (selectedRun as any)?.context ?? {};
  const a2aMessages = runContext.a2a_messages || [];
  const orchestratorRouting = runContext.orchestrator_routing;
  // Terminal runs have no live agents — older snapshots (cancelled before
  // the server cleared statuses) can still say "running"; never pulse them.
  const runTerminal = ["completed", "failed", "stage_failed", "agent_timeout", "cancelled"].includes(
    String((selectedRun as any)?.state ?? ""),
  );
  const displayAgents: PipelineRun["agents"] = runTerminal
    ? Object.fromEntries(
        Object.entries(selectedRun?.agents ?? {}).map(([k, v]) => [
          k,
          v?.status === "running" || v?.status === "in_progress" ? { ...v, status: "cancelled" } : v,
        ]),
      )
    : (selectedRun?.agents ?? {});

  return (
    <div className="flex h-full" style={{ background: "var(--canvas)" }}>
      {/* Recent runs column — sits inside the page (not the outer
       * mission-control nav) because this column is specific to the
       * Fleet view; other top-level pages (Registry, Blueprint) don't
       * need a runs list. */}
      <aside
        className="w-64 shrink-0 flex flex-col border-r"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        <div className="px-4 pt-4 pb-3">
          <button onClick={() => setTriggerOpen(true)} className="btn-primary w-full !py-2">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New Pipeline Run
          </button>
        </div>

        <div className="px-4 pt-1 pb-2 flex items-center justify-between">
          <span className="label-eyebrow">Recent Runs</span>
          <Link
            href="/runs"
            className="text-[11px] font-medium underline-offset-2 hover:underline"
            style={{ color: "var(--accent)" }}
          >
            View all →
          </Link>
        </div>

        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {loading ? (
            <div className="text-center py-8 text-xs" style={{ color: "var(--ink-muted)" }}>
              Loading…
            </div>
          ) : runs.length === 0 ? (
            <div className="text-center py-12 px-3">
              <p className="text-sm" style={{ color: "var(--ink-soft)" }}>
                No pipeline runs yet
              </p>
              <p className="text-xs mt-1" style={{ color: "var(--ink-muted)" }}>
                Trigger a run from the CLI or webhook
              </p>
            </div>
          ) : (
            <RunList
              runs={runs}
              selectedRunId={selectedRunId}
              onSelect={setSelectedRunId}
              onRetrigger={handleRetrigger}
              onResumeFailed={handleResumeFailed}
              onPause={handlePause}
              onResume={handleResume}
              onStop={handleStop}
              onDelete={handleDelete}
            />
          )}
        </div>

        <div
          className="px-4 py-2.5 border-t flex items-center justify-between text-xs"
          style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
        >
          <span className="flex items-center gap-1.5">
            <span className="dot dot-ok" />
            Supervisor + Orchestrator
          </span>
          <a href="/auth/logout" className="hover:underline" style={{ color: "var(--ink-muted)" }}>
            Logout
          </a>
        </div>
      </aside>

      {/* Detail pane — task timeline / agent grid / chat / etc. */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {selectedRun ? (
          <>
            <header
              className="border-b px-6 py-3 shrink-0"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                    {selectedRun.repo}
                  </h2>
                  <p className="text-xs font-mono mt-0.5" style={{ color: "var(--ink-muted)" }}>
                    {selectedRun.run_id}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {live && (
                    <span
                      className="pill inline-flex items-center gap-1.5"
                      title="Streaming live run events"
                      style={{ color: "var(--ok-ink)" }}
                    >
                      <span
                        className="inline-block w-1.5 h-1.5 rounded-full animate-pulse"
                        style={{ background: "var(--ok)" }}
                        aria-hidden
                      />
                      LIVE
                    </span>
                  )}
                  {orchestratorRouting?.progress_pct !== undefined && (
                    <span className="pill">{orchestratorRouting.progress_pct}%</span>
                  )}
                  <span className="pill-muted pill">{selectedRun.stage.replace(/_/g, " ")}</span>
                  <Link
                    href={`/runs/${encodeURIComponent(selectedRun.run_id)}`}
                    className="btn-secondary !py-1 !px-2.5 !text-xs"
                  >
                    Open run →
                  </Link>
                </div>
              </div>
            </header>

            <div
              className="border-b px-6 shrink-0"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <PipelineFlow
                blueprint={(selectedRun as any)?.blueprint || "alm-pipeline"}
                currentStage={selectedRun.stage}
                run={selectedRun as any}
                agentTimings={(selectedRun as any)?.context?.agent_timings}
              />
            </div>

            {approvals.length > 0 && (
              <div className="px-6 pt-4 shrink-0">
                <ApprovalBanner
                  approvals={approvals}
                  onApprove={handleApprove}
                  onReject={handleReject}
                />
              </div>
            )}

            <div
              className="border-b px-6 shrink-0"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="flex gap-0 -mb-px">
                {([
                  { key: "overview", label: "Overview" },
                  { key: "hierarchy", label: "Agent Hierarchy" },
                  { key: "agents", label: "Agents" },
                  { key: "a2a", label: "A2A Messages" },
                  { key: "events", label: "Events" },
                  { key: "chat", label: "Chat" },
                  { key: "config", label: "Config" },
                ] as const).map((t) => {
                  const isActive = tab === t.key;
                  return (
                    <button
                      key={t.key}
                      onClick={() => setTab(t.key)}
                      className="px-3.5 py-2.5 text-sm font-medium border-b-2 transition-colors"
                      style={{
                        borderColor: isActive ? "var(--accent)" : "transparent",
                        color: isActive ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                      }}
                    >
                      {t.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-6" style={{ background: "var(--canvas)" }}>
              {tab === "overview" && (
                <OverviewTab
                  run={{ ...selectedRun, agents: displayAgents }}
                  a2aMessages={a2aMessages}
                  orchestratorRouting={orchestratorRouting}
                />
              )}
              {tab === "hierarchy" && (
                <AgentHierarchy
                  agentStatuses={displayAgents}
                  a2aMessages={a2aMessages}
                  orchestratorRouting={orchestratorRouting}
                />
              )}
              {tab === "agents" && <AgentsTab run={{ ...selectedRun, agents: displayAgents }} />}
              {tab === "a2a" && <A2ATab messages={a2aMessages} />}
              {tab === "events" && <EventsTab events={(selectedRun as any)?.events || []} />}
              {tab === "chat" && <ChatPanel runId={selectedRun?.run_id} repo={selectedRun?.repo} stage={selectedRun?.stage} />}
              {tab === "config" && <ConfigTab repo={selectedRun?.repo || "default"} />}
            </div>
          </>
        ) : (
          // Empty state — shown when no run is selected. Editorial
          // treatment: paper-backed icon well, serif headline, sober
          // ink primary button. Aligns with the mark8ly admin look —
          // an admin tool that doesn't shout.
          <div className="flex-1 flex items-center justify-center" style={{ background: "var(--canvas)" }}>
            <div className="text-center max-w-md px-6">
              <div
                className="w-14 h-14 rounded-lg flex items-center justify-center mx-auto mb-5"
                style={{
                  background: "var(--surface)",
                  border: "1px solid var(--border-subtle)",
                  boxShadow: "var(--shadow-card)",
                }}
              >
                <svg
                  width="22"
                  height="22"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  style={{ color: "var(--ink-strong)" }}
                >
                  <path d="M12 2L2 7l10 5 10-5-10-5z" />
                  <path d="M2 17l10 5 10-5" />
                  <path d="M2 12l10 5 10-5" />
                </svg>
              </div>
              <h2 className="font-serif text-xl font-medium tracking-tight" style={{ color: "var(--ink-strong)" }}>
                DevAI Multi-Agent Platform
              </h2>
              <p className="text-sm mt-2 leading-relaxed" style={{ color: "var(--ink-soft)" }}>
                Supervisor &rarr; Orchestrator &rarr; Specialist Agents. AI-powered Application Lifecycle Management.
              </p>
              <button onClick={() => setTriggerOpen(true)} className="btn-primary mt-6">
                Start new pipeline
              </button>
            </div>
          </div>
        )}
      </main>

      <TriggerDialog
        open={triggerOpen}
        onClose={() => setTriggerOpen(false)}
        onTrigger={handleTrigger}
      />
    </div>
  );
}

// --- Tab Components ---

function OverviewTab({
  run,
  a2aMessages,
  orchestratorRouting,
}: {
  run: PipelineRun;
  a2aMessages: any[];
  orchestratorRouting?: any;
}) {
  const agents = run.agents || {};
  const coordinators = Object.entries(AGENT_INFO).filter(([, info]) => info.role === "coordinator");
  const specialists = Object.entries(AGENT_INFO).filter(([, info]) => info.role === "specialist");

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          Coordination Layer
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {coordinators.map(([key]) => {
            const agentStatus = agents[key];
            const msgCount = a2aMessages.filter(
              (m: any) => m.from_agent === key || m.to_agent === key
            ).length;
            return (
              <AgentCard
                key={key}
                agentKey={key}
                status={agentStatus?.status}
                error={agentStatus?.error}
                messageCount={msgCount}
              />
            );
          })}
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          Specialist Agents
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          {specialists.map(([key]) => {
            const agentStatus = agents[key];
            const msgCount = a2aMessages.filter(
              (m: any) => m.from_agent === key || m.to_agent === key
            ).length;
            return (
              <AgentCard
                key={key}
                agentKey={key}
                status={agentStatus?.status}
                error={agentStatus?.error}
                messageCount={msgCount}
              />
            );
          })}
        </div>
      </div>

      {a2aMessages.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
            Recent Agent Communication
          </h3>
          <A2AFeed messages={a2aMessages.slice(-5)} />
        </div>
      )}
    </div>
  );
}

function AgentsTab({ run }: { run: PipelineRun }) {
  const agents = run.agents || {};
  const coordinators = Object.entries(AGENT_INFO).filter(([, info]) => info.role === "coordinator");
  const specialists = Object.entries(AGENT_INFO).filter(([, info]) => info.role === "specialist");

  const renderAgentDetail = ([key, info]: [string, typeof AGENT_INFO[string]]) => {
    const agentStatus = agents[key];
    return (
      <div
        key={key}
        className="p-4 rounded-lg border border-[var(--border)] bg-[var(--surface)]"
      >
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center text-base font-semibold shrink-0"
            style={{ backgroundColor: `${info.color}18`, color: info.color }}
          >
            {info.label.charAt(0)}
          </div>
          <div>
            <h4 className="text-sm font-medium text-[var(--ink-strong)]">{info.label}</h4>
            <p className="text-xs text-[var(--ink-muted)]">
              {info.provider} &middot; {info.role}
            </p>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <StatusBadgeInline status={agentStatus?.status} color={info.color} />
          {info.role === "coordinator" && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 font-medium border border-indigo-100 dark:border-indigo-900">
              COORDINATOR
            </span>
          )}
          {agentStatus?.error && (
            <span className="text-xs text-red-600 dark:text-red-400 truncate">{agentStatus.error}</span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold text-indigo-600 dark:text-indigo-400 uppercase tracking-wider mb-3">
          Coordinators ({coordinators.length})
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {coordinators.map(renderAgentDetail)}
        </div>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          Specialists ({specialists.length})
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {specialists.map(renderAgentDetail)}
        </div>
      </div>
    </div>
  );
}

function StatusBadgeInline({ status, color }: { status?: string; color: string }) {
  if (!status) return <span className="text-xs px-2 py-0.5 rounded-md bg-[var(--surface-hover)] text-[var(--ink-muted)]">Idle</span>;

  const styles: Record<string, string> = {
    completed: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-400 border border-green-100 dark:border-green-900",
    running: "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-400 border border-blue-100 dark:border-blue-900",
    failed: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400 border border-red-100 dark:border-red-900",
    waiting_approval: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border border-amber-100 dark:border-amber-900",
  };

  const labels: Record<string, string> = {
    completed: "Done",
    running: "Running",
    failed: "Failed",
    waiting_approval: "Awaiting",
  };

  return (
    <span className={`text-xs px-2 py-0.5 rounded-md font-medium ${styles[status] || styles.running}`}>
      {labels[status] || status}
    </span>
  );
}

function A2ATab({ messages }: { messages: any[] }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider">
          Agent-to-Agent Messages ({messages.length})
        </h3>
      </div>
      <A2AFeed messages={messages} />
    </div>
  );
}

function EventsTab({ events }: { events: Array<{ step: string; status: string; detail: string; timestamp: number }> }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider">
          Pipeline Events ({events.length})
        </h3>
      </div>
      {events.length === 0 ? (
        <div className="text-center py-8 text-[var(--ink-muted)] text-sm">
          No events recorded yet
        </div>
      ) : (
        <div className="space-y-1 max-h-[600px] overflow-y-auto">
          {[...events].reverse().map((evt, i) => {
            const isError = evt.status === "failed";
            const isRunning = evt.status === "running";
            return (
              <div
                key={i}
                className={`flex items-start gap-3 p-3 rounded-lg border transition-colors ${
                  isError
                    ? "bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-900"
                    : isRunning
                      ? "bg-indigo-50 dark:bg-indigo-950/30 border-indigo-200 dark:border-indigo-900"
                      : "bg-[var(--surface)] border-[var(--border)]"
                }`}
              >
                <div className={`mt-0.5 w-2 h-2 rounded-full shrink-0 ${
                  isError ? "bg-red-500" : isRunning ? "bg-indigo-500 animate-pulse" : "bg-green-500"
                }`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-[var(--ink)]">
                      {evt.step.replace(/_/g, " ")}
                    </span>
                    <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${
                      isError
                        ? "bg-red-100 dark:bg-red-900/50 text-red-600 dark:text-red-400"
                        : isRunning
                          ? "bg-indigo-100 dark:bg-indigo-900/50 text-indigo-600 dark:text-indigo-400"
                          : "bg-green-100 dark:bg-green-900/50 text-green-600 dark:text-green-400"
                    }`}>
                      {evt.status}
                    </span>
                  </div>
                  <p className="text-xs text-[var(--ink-muted)] mt-0.5 break-words">
                    {evt.detail}
                  </p>
                </div>
                <span className="text-xs text-[var(--ink-muted)] shrink-0 whitespace-nowrap">
                  {new Date(evt.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ConfigTab({ repo }: { repo: string }) {
  const [config, setConfig] = useState({
    auto_mode: false,
    gates: {
      deployment: true,
      testing: true,
      review: false,
      merge: true,
      createPR: false,
    },
    claude_model: "claude-sonnet-4-20250514",
    openai_model: "o3",
    groq_model: "llama-3.3-70b-versatile",
    max_review_iterations: 3,
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [loaded, setLoaded] = useState(false);

  // Load config on mount
  useEffect(() => {
    api.getConfig(repo).then((data) => {
      setConfig((prev) => ({
        ...prev,
        auto_mode: data.auto_mode ?? prev.auto_mode,
        claude_model: data.claude_model ?? prev.claude_model,
        openai_model: data.openai_model ?? prev.openai_model,
        groq_model: (data as any).groq_model ?? prev.groq_model,
        max_review_iterations: data.max_review_iterations ?? prev.max_review_iterations,
        gates: { ...prev.gates, ...(data.gates || {}) },
      }));
      setLoaded(true);
    }).catch(() => setLoaded(true));
  }, [repo]);

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await api.saveConfig({ ...config, repo });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {
      // Handle error silently
    } finally {
      setSaving(false);
    }
  };

  const gateDescriptions: Record<string, string> = {
    deployment: "Pause before deploying to production via ArgoCD",
    testing: "Pause before running E2E tests on story PRs",
    review: "Pause before submitting code review on PRs",
    merge: "Pause before merging approved story PRs to main",
    createPR: "Pause before creating pull requests for stories",
  };

  const providers = [
    {
      dot: "bg-indigo-500",
      name: "Claude (Anthropic)",
      key: "claude_model" as const,
      desc: "EM, Developer, DB, Security, QA, Infra",
    },
    {
      dot: "bg-green-600",
      name: "OpenAI",
      key: "openai_model" as const,
      desc: "Supervisor, Orchestrator, Product Director, Staff Reviewer",
    },
    {
      dot: "bg-orange-500",
      name: "Groq (Llama)",
      key: "groq_model" as const,
      desc: "Doc Analyzer, Tech Detector, Requirements, CI Monitor, Release",
    },
  ];

  if (!loaded) return <div className="text-sm text-[var(--ink-muted)]">Loading config...</div>;

  return (
    <div className="w-full max-w-3xl space-y-6">
      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          Pipeline Configuration
        </h3>
        <div className="space-y-3 p-4 rounded-lg border border-[var(--border)] bg-[var(--surface)]">
          <label className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium text-[var(--ink)]">Auto Mode</span>
              <p className="text-xs text-[var(--ink-muted)] mt-0.5">Skip all approval gates — pipeline runs fully autonomous</p>
            </div>
            <input
              type="checkbox"
              checked={config.auto_mode}
              onChange={(e) => setConfig({ ...config, auto_mode: e.target.checked })}
              className="w-4 h-4 rounded accent-indigo-600"
            />
          </label>
          <div className="border-t border-[var(--border-subtle)] pt-3">
            <label className="flex items-center justify-between">
              <div>
                <span className="text-sm font-medium text-[var(--ink)]">Max Review Iterations</span>
                <p className="text-xs text-[var(--ink-muted)] mt-0.5">How many review/fix cycles before escalating</p>
              </div>
              <input
                type="number"
                min={1}
                max={10}
                value={config.max_review_iterations}
                onChange={(e) => setConfig({ ...config, max_review_iterations: parseInt(e.target.value) || 3 })}
                className="w-16 px-2 py-1 rounded bg-[var(--surface-muted)] border border-[var(--border)] text-sm text-right text-[var(--ink-strong)] focus:outline-none focus:border-indigo-400"
              />
            </label>
          </div>
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          Approval Gates
        </h3>
        <div className="divide-y divide-[var(--border-subtle)] rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
          {Object.entries(config.gates).map(([gate, enabled]) => (
            <label key={gate} className="flex items-center justify-between px-4 py-3 hover:bg-[var(--surface-hover)] transition-colors cursor-pointer">
              <div>
                <span className="text-sm text-[var(--ink-soft)] capitalize">{gate.replace(/([A-Z])/g, " $1")}</span>
                <p className="text-xs text-[var(--ink-muted)] mt-0.5">{gateDescriptions[gate] || ""}</p>
              </div>
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    gates: { ...config.gates, [gate]: e.target.checked },
                  })
                }
                className="w-4 h-4 rounded accent-indigo-600"
              />
            </label>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-[var(--ink-muted)] uppercase tracking-wider mb-3">
          LLM Providers
        </h3>
        <div className="divide-y divide-[var(--border-subtle)] rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
          {providers.map((p) => (
            <div key={p.name} className="px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 shrink-0">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${p.dot}`} />
                  <span className="text-sm text-[var(--ink-soft)]">{p.name}</span>
                </div>
                <input
                  type="text"
                  value={(config as any)[p.key] || ""}
                  onChange={(e) => setConfig({ ...config, [p.key]: e.target.value })}
                  className="w-52 px-2 py-1 rounded bg-[var(--surface-muted)] border border-[var(--border)] text-xs font-mono text-right text-[var(--ink-strong)] focus:outline-none focus:border-indigo-400"
                />
              </div>
              <p className="text-xs text-[var(--ink-muted)] mt-1 ml-4">{p.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Save Button */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {saving ? (
            <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" className="opacity-25" /><path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" className="opacity-75" /></svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z" /><polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" /></svg>
          )}
          {saving ? "Saving..." : "Save Configuration"}
        </button>
        {saved && (
          <span className="text-sm text-green-500 flex items-center gap-1">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
            Saved
          </span>
        )}
      </div>
      <p className="text-xs text-[var(--ink-muted)]">
        Configuration is saved per repository ({repo}). Changes take effect on the next pipeline run.
      </p>
    </div>
  );
}
