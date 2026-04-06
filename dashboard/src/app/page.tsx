"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type PipelineRun } from "@/lib/api";
import { AGENT_INFO } from "@/lib/constants";
import { PipelineFlow } from "@/components/pipeline-flow";
import { AgentCard } from "@/components/agent-card";
import { A2AFeed } from "@/components/a2a-feed";
import { RunList } from "@/components/run-list";
import { TriggerDialog } from "@/components/trigger-dialog";
import { ApprovalBanner } from "@/components/approval-banner";
import { ChatPanel } from "@/components/chat-panel";

type Tab = "overview" | "agents" | "a2a" | "chat" | "config";

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

  // Mock A2A messages from run context (in production, served from API)
  const a2aMessages = (selectedRun as any)?.context?.a2a_messages || [];

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-72 border-r border-[var(--border-primary)] bg-[var(--bg-secondary)] flex flex-col">
        {/* Header */}
        <div className="p-4 border-b border-[var(--border-primary)]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-sm font-bold">
              D
            </div>
            <div>
              <h1 className="text-sm font-bold text-[var(--text-primary)]">DevAI</h1>
              <p className="text-[10px] text-[var(--text-muted)]">ALM Pipeline Dashboard</p>
            </div>
          </div>
        </div>

        {/* New Run Button */}
        <div className="p-3">
          <button
            onClick={() => setTriggerOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-500 transition-colors"
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

        {/* Footer */}
        <div className="p-3 border-t border-[var(--border-primary)]">
          <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
            <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
            LangGraph + LangSmith
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
                        ? "border-blue-500 text-blue-400"
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
                <OverviewTab run={selectedRun} a2aMessages={a2aMessages} />
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
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-500/20 to-purple-600/20 border border-blue-500/30 flex items-center justify-center mx-auto mb-4">
                <span className="text-2xl">🤖</span>
              </div>
              <h2 className="text-lg font-semibold text-[var(--text-primary)]">DevAI Dashboard</h2>
              <p className="text-sm text-[var(--text-muted)] mt-1 max-w-sm">
                AI-powered Application Lifecycle Management.
                Select a run or trigger a new pipeline.
              </p>
              <button
                onClick={() => setTriggerOpen(true)}
                className="mt-4 px-4 py-2 text-xs font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-500 transition-colors"
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

function OverviewTab({ run, a2aMessages }: { run: PipelineRun; a2aMessages: any[] }) {
  const agents = run.agents || {};
  const agentKeys = Object.keys(AGENT_INFO);

  return (
    <div className="space-y-6">
      {/* Agent Grid */}
      <div>
        <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
          Agent Status
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          {agentKeys.map((key) => {
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

  return (
    <div className="space-y-4">
      <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">
        All Agents ({Object.keys(AGENT_INFO).length})
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {Object.entries(AGENT_INFO).map(([key, info]) => {
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
                  <p className="text-xs text-[var(--text-muted)]">Provider: {info.provider}</p>
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
                {agentStatus?.error && (
                  <span className="text-[10px] text-red-400 truncate">{agentStatus.error}</span>
                )}
              </div>
            </div>
          );
        })}
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
              className="w-4 h-4 rounded accent-blue-500"
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
                className="w-4 h-4 rounded accent-blue-500"
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
            <span className="text-[var(--text-secondary)]">Claude (Anthropic)</span>
            <span className="text-[var(--text-muted)] font-mono text-xs">claude-sonnet-4</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">OpenAI (Codex)</span>
            <span className="text-[var(--text-muted)] font-mono text-xs">o3</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-[var(--text-secondary)]">Groq (Llama 3.3)</span>
            <span className="text-[var(--text-muted)] font-mono text-xs">llama-3.3-70b</span>
          </div>
        </div>
      </div>
    </div>
  );
}
