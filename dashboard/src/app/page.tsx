"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type PipelineRun } from "@/lib/api";
import { AGENT_INFO } from "@/lib/constants";
import { PipelineFlow } from "@/components/pipeline-flow";
import { AgentCard } from "@/components/agent-card";
import { AgentHierarchy } from "@/components/agent-hierarchy";
import { A2AFeed } from "@/components/a2a-feed";
import { RunList } from "@/components/run-list";
import { TriggerDialog } from "@/components/trigger-dialog";
import { ApprovalBanner } from "@/components/approval-banner";
import { ChatPanel } from "@/components/chat-panel";

type Tab = "overview" | "hierarchy" | "agents" | "a2a" | "chat" | "config";

export default function DashboardPage() {
  const [runs, setRuns] = useState<PipelineRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [selectedRun, setSelectedRun] = useState<PipelineRun | null>(null);
  const [approvals, setApprovals] = useState<Array<{ gate: string; agent: string; timestamp: number }>>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [triggerOpen, setTriggerOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  // Fetch runs on mount and periodically
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

  // Fetch selected run details
  useEffect(() => {
    if (!selectedRunId) return;
    const fetchDetail = async () => {
      try {
        const run = await api.getRun(selectedRunId);
        setSelectedRun(run);
        const pendingApprovals = await api.getApprovals(selectedRunId);
        setApprovals(pendingApprovals);
      } catch {
        // Run may not exist
      }
    };
    fetchDetail();
    const interval = setInterval(fetchDetail, 3000);
    return () => clearInterval(interval);
  }, [selectedRunId]);

  const handleTrigger = async (repo: string, requirements: string) => {
    const result = await api.triggerPipeline(repo, requirements);
    setSelectedRunId(result.run_id);
    await fetchRuns();
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

  // Extract context data from run
  const runContext = (selectedRun as any)?.context ?? {};
  const a2aMessages = runContext.a2a_messages || [];
  const orchestratorRouting = runContext.orchestrator_routing;

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-72 border-r border-[var(--border-primary)] bg-[var(--bg-secondary)] flex flex-col">
        {/* Header */}
        <div className="p-4 border-b border-[var(--border-primary)]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-white text-sm font-bold">
              D
            </div>
            <div>
              <h1 className="text-sm font-bold text-[var(--text-primary)]">DevAI</h1>
              <p className="text-[10px] text-[var(--text-muted)]">Multi-Agent ALM Platform</p>
            </div>
          </div>
        </div>

        {/* New Run Button */}
        <div className="p-3">
          <button
            onClick={() => setTriggerOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium rounded-lg bg-gradient-to-r from-violet-600 to-indigo-600 text-white hover:from-violet-500 hover:to-indigo-500 transition-all shadow-sm"
          >
            <span>+</span>
            New Pipeline Run
          </button>
        </div>

        {/* Run List */}
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {loading ? (
            <div className="text-center py-8 text-[var(--text-muted)] text-xs">Loading...</div>
          ) : (
            <RunList runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
          )}
        </div>

        {/* User + Logout */}
        <div className="p-3 border-t border-[var(--border-primary)]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
              <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
              Supervisor + Orchestrator
            </div>
            <a
              href="/bff/logout"
              className="text-[10px] text-[var(--text-muted)] hover:text-red-400 transition-colors"
            >
              Logout
            </a>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {selectedRun ? (
          <>
            {/* Top Bar */}
            <header className="border-b border-[var(--border-primary)] bg-[var(--bg-secondary)] px-6 py-3">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                    {selectedRun.repo}
                  </h2>
                  <p className="text-[10px] font-mono text-[var(--text-muted)] mt-0.5">
                    {selectedRun.run_id}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {orchestratorRouting?.progress_pct !== undefined && (
                    <span className="text-[10px] px-2 py-1 rounded-full bg-indigo-500/15 text-indigo-400 font-mono">
                      {orchestratorRouting.progress_pct}%
                    </span>
                  )}
                  <span className="text-xs px-2.5 py-1 rounded-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)] font-medium">
                    {selectedRun.stage.replace(/_/g, " ")}
                  </span>
                </div>
              </div>
            </header>

            {/* Pipeline Flow */}
            <div className="border-b border-[var(--border-primary)] bg-[var(--bg-secondary)] px-6">
              <PipelineFlow
                currentStage={selectedRun.stage}
                agentTimings={(selectedRun as any)?.context?.agent_timings}
              />
            </div>

            {/* Approval Banners */}
            {approvals.length > 0 && (
              <div className="px-6 pt-4">
                <ApprovalBanner
                  approvals={approvals}
                  onApprove={handleApprove}
                  onReject={handleReject}
                />
              </div>
            )}

            {/* Tabs */}
            <div className="border-b border-[var(--border-primary)] px-6">
              <div className="flex gap-1">
                {([
                  { key: "overview", label: "Overview" },
                  { key: "hierarchy", label: "Agent Hierarchy" },
                  { key: "agents", label: "Agents" },
                  { key: "a2a", label: "A2A Messages" },
                  { key: "chat", label: "Chat" },
                  { key: "config", label: "Config" },
                ] as const).map((t) => (
                  <button
                    key={t.key}
                    onClick={() => setTab(t.key)}
                    className={`px-3 py-2.5 text-xs font-medium border-b-2 transition-colors ${
                      tab === t.key
                        ? "border-violet-500 text-violet-400"
                        : "border-transparent text-[var(--text-muted)] hover:text-[var(--text-secondary)]"
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Tab Content */}
            <div className="flex-1 overflow-y-auto p-6">
              {tab === "overview" && (
                <OverviewTab run={selectedRun} a2aMessages={a2aMessages} orchestratorRouting={orchestratorRouting} />
              )}
              {tab === "hierarchy" && (
                <AgentHierarchy
                  agentStatuses={selectedRun.agents || {}}
                  a2aMessages={a2aMessages}
                  orchestratorRouting={orchestratorRouting}
                />
              )}
              {tab === "agents" && <AgentsTab run={selectedRun} />}
              {tab === "a2a" && <A2ATab messages={a2aMessages} />}
              {tab === "chat" && <ChatPanel />}
              {tab === "config" && <ConfigTab />}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-violet-500/20 to-indigo-600/20 border border-violet-500/30 flex items-center justify-center mx-auto mb-4">
                <span className="text-2xl">🧠</span>
              </div>
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">DevAI Multi-Agent Platform</h2>
              <p className="text-sm text-[var(--text-muted)] mt-1 max-w-sm">
                Supervisor &rarr; Orchestrator &rarr; Specialist Agents.
                AI-powered Application Lifecycle Management.
              </p>
              <button
                onClick={() => setTriggerOpen(true)}
                className="mt-4 px-4 py-2 text-xs font-medium rounded-lg bg-gradient-to-r from-violet-600 to-indigo-600 text-white hover:from-violet-500 hover:to-indigo-500 transition-all"
              >
                Start New Pipeline
              </button>
            </div>
          </div>
        )}
      </main>

      {/* Trigger Dialog */}
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
      {/* Coordinator Agents */}
      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
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

      {/* Specialist Agents */}
      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
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

      {/* Recent A2A Activity */}
      {a2aMessages.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
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
        className="p-4 rounded-lg border border-[var(--border-primary)] bg-[var(--bg-secondary)]"
      >
        <div className="flex items-center gap-3">
          <span className="text-2xl">{info.icon}</span>
          <div>
            <h4 className="text-sm font-semibold text-[var(--text-primary)]">{info.label}</h4>
            <p className="text-xs text-[var(--text-muted)]">
              Provider: {info.provider} | Role: {info.role}
            </p>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <span
            className="text-[10px] px-2 py-0.5 rounded-full font-medium"
            style={{
              backgroundColor: `${info.color}15`,
              color: info.color,
            }}
          >
            {agentStatus?.status || "idle"}
          </span>
          {info.role === "coordinator" && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-300 font-medium">
              COORDINATOR
            </span>
          )}
          {agentStatus?.error && (
            <span className="text-[10px] text-red-400 truncate">{agentStatus.error}</span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-violet-400 uppercase tracking-wider mb-3">
          Coordinators ({coordinators.length})
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {coordinators.map(renderAgentDetail)}
        </div>
      </div>
      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
          Specialists ({specialists.length})
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {specialists.map(renderAgentDetail)}
        </div>
      </div>
    </div>
  );
}

function A2ATab({ messages }: { messages: any[] }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">
          Agent-to-Agent Messages ({messages.length})
        </h3>
      </div>
      <A2AFeed messages={messages} />
    </div>
  );
}

function ConfigTab() {
  const [config, setConfig] = useState({
    auto_mode: false,
    gates: {
      deployment: true,
      testing: true,
      review: false,
      merge: true,
      createPR: false,
    },
  });

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
          Pipeline Configuration
        </h3>
        <div className="space-y-4 p-4 rounded-lg border border-[var(--border-primary)] bg-[var(--bg-secondary)]">
          <label className="flex items-center justify-between">
            <div>
              <span className="text-sm text-[var(--text-primary)]">Auto Mode</span>
              <p className="text-[10px] text-[var(--text-muted)]">Skip all approval gates</p>
            </div>
            <input
              type="checkbox"
              checked={config.auto_mode}
              onChange={(e) => setConfig({ ...config, auto_mode: e.target.checked })}
              className="w-4 h-4 rounded accent-violet-500"
            />
          </label>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
          Approval Gates
        </h3>
        <div className="space-y-2 p-4 rounded-lg border border-[var(--border-primary)] bg-[var(--bg-secondary)]">
          {Object.entries(config.gates).map(([gate, enabled]) => (
            <label key={gate} className="flex items-center justify-between py-1">
              <span className="text-sm text-[var(--text-primary)] capitalize">{gate.replace(/([A-Z])/g, " $1")}</span>
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    gates: { ...config.gates, [gate]: e.target.checked },
                  })
                }
                className="w-4 h-4 rounded accent-violet-500"
              />
            </label>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
          LLM Providers
        </h3>
        <div className="space-y-3 p-4 rounded-lg border border-[var(--border-primary)] bg-[var(--bg-secondary)]">
          <div className="flex items-center justify-between text-sm">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-violet-500" />
              <span className="text-[var(--text-secondary)]">Claude (Anthropic)</span>
            </div>
            <span className="text-[var(--text-muted)] font-mono text-xs">claude-sonnet-4</span>
          </div>
          <div className="text-[10px] text-[var(--text-muted)] ml-4 -mt-1">
            Supervisor, Orchestrator, EM, Developer, DB, Security, QA, Infra
          </div>
          <div className="flex items-center justify-between text-sm">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-emerald-500" />
              <span className="text-[var(--text-secondary)]">OpenAI (Codex)</span>
            </div>
            <span className="text-[var(--text-muted)] font-mono text-xs">o3</span>
          </div>
          <div className="text-[10px] text-[var(--text-muted)] ml-4 -mt-1">
            Product Director, Staff Reviewer
          </div>
          <div className="flex items-center justify-between text-sm">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-cyan-500" />
              <span className="text-[var(--text-secondary)]">Groq (Llama 3.3)</span>
            </div>
            <span className="text-[var(--text-muted)] font-mono text-xs">llama-3.3-70b</span>
          </div>
          <div className="text-[10px] text-[var(--text-muted)] ml-4 -mt-1">
            Doc Analyzer, Tech Detector, Requirements, CI Monitor, Release
          </div>
        </div>
      </div>
    </div>
  );
}
