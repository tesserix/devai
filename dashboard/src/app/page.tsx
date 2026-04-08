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

  const handleRetrigger = async (repo: string) => {
    const result = await api.triggerPipeline(repo, "Retry failed pipeline run");
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

  const runContext = (selectedRun as any)?.context ?? {};
  const a2aMessages = runContext.a2a_messages || [];
  const orchestratorRouting = runContext.orchestrator_routing;

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      {/* Sidebar */}
      <aside className="w-68 shrink-0 border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex flex-col" style={{ width: "272px" }}>
        {/* Header */}
        <div className="px-4 py-3.5 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-md bg-indigo-600 flex items-center justify-center text-white text-xs font-bold shrink-0">
              D
            </div>
            <div>
              <h1 className="text-sm font-semibold text-gray-900 dark:text-gray-100">DevAI</h1>
              <p className="text-xs text-gray-400 dark:text-gray-500">Multi-Agent ALM Platform</p>
            </div>
          </div>
        </div>

        {/* New Run Button */}
        <div className="px-3 pt-3 pb-2">
          <button
            onClick={() => setTriggerOpen(true)}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-medium rounded-md bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            New Pipeline Run
          </button>
        </div>

        {/* Runs label */}
        <div className="px-4 pt-2 pb-1">
          <span className="text-xs font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider">Recent Runs</span>
        </div>

        {/* Run List */}
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {loading ? (
            <div className="text-center py-8 text-gray-400 dark:text-gray-500 text-xs">Loading...</div>
          ) : (
            <RunList runs={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} onRetrigger={handleRetrigger} />
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
              <span className="w-1.5 h-1.5 rounded-full bg-green-600" />
              Supervisor + Orchestrator
            </div>
            <a
              href="/bff/logout"
              className="text-xs text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 transition-colors"
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
            <header className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-6 py-3 shrink-0">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                    {selectedRun.repo}
                  </h2>
                  <p className="text-xs font-mono text-gray-400 dark:text-gray-500 mt-0.5">
                    {selectedRun.run_id}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {orchestratorRouting?.progress_pct !== undefined && (
                    <span className="text-xs px-2 py-0.5 rounded-md bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400 font-mono border border-indigo-100 dark:border-indigo-900">
                      {orchestratorRouting.progress_pct}%
                    </span>
                  )}
                  <span className="text-xs px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 font-medium border border-gray-200 dark:border-gray-600">
                    {selectedRun.stage.replace(/_/g, " ")}
                  </span>
                </div>
              </div>
            </header>

            {/* Pipeline Flow */}
            <div className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-6 shrink-0">
              <PipelineFlow
                currentStage={selectedRun.stage}
                agentTimings={(selectedRun as any)?.context?.agent_timings}
              />
            </div>

            {/* Approval Banners */}
            {approvals.length > 0 && (
              <div className="px-6 pt-4 shrink-0">
                <ApprovalBanner
                  approvals={approvals}
                  onApprove={handleApprove}
                  onReject={handleReject}
                />
              </div>
            )}

            {/* Tabs */}
            <div className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-6 shrink-0">
              <div className="flex gap-0 -mb-px">
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
                    className={`px-3.5 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                      tab === t.key
                        ? "border-indigo-600 text-indigo-600 dark:text-indigo-400 dark:border-indigo-400"
                        : "border-transparent text-gray-500 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Tab Content */}
            <div className="flex-1 overflow-y-auto p-6 bg-gray-50 dark:bg-gray-900">
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
          <div className="flex-1 flex items-center justify-center bg-gray-50 dark:bg-gray-900">
            <div className="text-center">
              <div className="w-14 h-14 rounded-xl bg-indigo-50 dark:bg-indigo-950 border border-indigo-100 dark:border-indigo-900 flex items-center justify-center mx-auto mb-4">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-indigo-600 dark:text-indigo-400">
                  <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
              </div>
              <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">DevAI Multi-Agent Platform</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 max-w-sm">
                Supervisor &rarr; Orchestrator &rarr; Specialist Agents.
                AI-powered Application Lifecycle Management.
              </p>
              <button
                onClick={() => setTriggerOpen(true)}
                className="mt-5 px-4 py-2 text-xs font-medium rounded-md bg-indigo-600 text-white hover:bg-indigo-700 transition-colors"
              >
                Start New Pipeline
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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
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
          <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
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
        className="p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
      >
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center text-base font-semibold shrink-0"
            style={{ backgroundColor: `${info.color}18`, color: info.color }}
          >
            {info.label.charAt(0)}
          </div>
          <div>
            <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">{info.label}</h4>
            <p className="text-xs text-gray-400 dark:text-gray-500">
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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
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
  if (!status) return <span className="text-xs px-2 py-0.5 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400">Idle</span>;

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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">
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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
          Pipeline Configuration
        </h3>
        <div className="space-y-3 p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
          <label className="flex items-center justify-between">
            <div>
              <span className="text-sm font-medium text-gray-800 dark:text-gray-200">Auto Mode</span>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Skip all approval gates</p>
            </div>
            <input
              type="checkbox"
              checked={config.auto_mode}
              onChange={(e) => setConfig({ ...config, auto_mode: e.target.checked })}
              className="w-4 h-4 rounded accent-indigo-600"
            />
          </label>
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
          Approval Gates
        </h3>
        <div className="divide-y divide-gray-100 dark:divide-gray-700 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden">
          {Object.entries(config.gates).map(([gate, enabled]) => (
            <label key={gate} className="flex items-center justify-between px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors cursor-pointer">
              <span className="text-sm text-gray-700 dark:text-gray-300 capitalize">{gate.replace(/([A-Z])/g, " $1")}</span>
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
        <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
          LLM Providers
        </h3>
        <div className="divide-y divide-gray-100 dark:divide-gray-700 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden">
          {[
            {
              dot: "bg-indigo-500",
              name: "Claude (Anthropic)",
              model: "claude-sonnet-4",
              desc: "Supervisor, Orchestrator, EM, Developer, DB, Security, QA, Infra",
            },
            {
              dot: "bg-green-600",
              name: "OpenAI (Codex)",
              model: "o3",
              desc: "Product Director, Staff Reviewer",
            },
            {
              dot: "bg-orange-500",
              name: "Groq (Llama 3.3)",
              model: "llama-3.3-70b",
              desc: "Doc Analyzer, Tech Detector, Requirements, CI Monitor, Release",
            },
          ].map((p) => (
            <div key={p.name} className="px-4 py-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${p.dot}`} />
                  <span className="text-sm text-gray-700 dark:text-gray-300">{p.name}</span>
                </div>
                <span className="text-xs font-mono text-gray-400 dark:text-gray-500">{p.model}</span>
              </div>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1 ml-4">{p.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
