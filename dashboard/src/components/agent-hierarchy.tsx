"use client";

import { clsx } from "clsx";
import { AGENT_INFO, EXECUTION_PHASES } from "@/lib/constants";

interface AgentHierarchyProps {
  agentStatuses: Record<string, { status?: string; error?: string }>;
  a2aMessages: Array<{ from_agent: string; to_agent: string; message_type: string; subject: string }>;
  orchestratorRouting?: {
    current_phase?: string;
    progress_pct?: number;
    decision?: string;
    status_summary?: string;
  };
}

function StatusDot({ status }: { status?: string }) {
  const color =
    status === "completed"
      ? "bg-green-500"
      : status === "running"
        ? "bg-blue-500 animate-pulse"
        : status === "failed"
          ? "bg-red-500"
          : status === "waiting_approval"
            ? "bg-amber-500 animate-pulse"
            : "bg-gray-600";

  return <span className={clsx("w-2 h-2 rounded-full inline-block", color)} />;
}

export function AgentHierarchy({ agentStatuses, a2aMessages, orchestratorRouting }: AgentHierarchyProps) {
  const supervisorInfo = AGENT_INFO["supervisor"];
  const orchestratorInfo = AGENT_INFO["orchestrator"];
  const supervisorStatus = agentStatuses["supervisor"]?.status;
  const orchestratorStatus = agentStatuses["orchestrator"]?.status;

  const progressPct = orchestratorRouting?.progress_pct ?? 0;
  const currentPhase = orchestratorRouting?.current_phase ?? "";

  return (
    <div className="space-y-6">
      {/* Progress Bar */}
      <div className="p-4 rounded-xl border border-[var(--border-primary)] bg-[var(--bg-secondary)]">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">
            Pipeline Progress
          </span>
          <span className="text-xs font-mono text-[var(--text-muted)]">{progressPct}%</span>
        </div>
        <div className="w-full h-2 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-violet-500 via-blue-500 to-emerald-500 transition-all duration-700"
            style={{ width: `${progressPct}%` }}
          />
        </div>
        {orchestratorRouting?.status_summary && (
          <p className="text-[10px] text-[var(--text-muted)] mt-2">{orchestratorRouting.status_summary}</p>
        )}
      </div>

      {/* Supervisor Node */}
      <div className="flex flex-col items-center">
        <div
          className={clsx(
            "relative w-full max-w-md p-4 rounded-xl border-2 transition-all",
            supervisorStatus === "running"
              ? "border-violet-500 bg-violet-500/10 shadow-lg shadow-violet-500/20"
              : supervisorStatus === "completed"
                ? "border-green-500/50 bg-green-500/5"
                : "border-[var(--border-secondary)] bg-[var(--bg-elevated)]"
          )}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-12 h-12 rounded-xl flex items-center justify-center text-2xl"
              style={{ backgroundColor: `${supervisorInfo.color}20` }}
            >
              {supervisorInfo.icon}
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-bold text-[var(--text-primary)]">{supervisorInfo.label}</h3>
                <StatusDot status={supervisorStatus} />
              </div>
              <p className="text-[10px] text-[var(--text-muted)]">Plans architecture &amp; delegates tasks</p>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-400 font-medium">
                  {supervisorInfo.provider}
                </span>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-300">
                  COORDINATOR
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Connector: Supervisor -> Orchestrator */}
        <div className="w-0.5 h-6 bg-gradient-to-b from-violet-500/50 to-indigo-500/50" />
        <div className="w-3 h-3 rotate-45 border-b-2 border-r-2 border-indigo-500/50 -mt-2" />
      </div>

      {/* Orchestrator Node */}
      <div className="flex flex-col items-center -mt-2">
        <div
          className={clsx(
            "relative w-full max-w-md p-4 rounded-xl border-2 transition-all",
            orchestratorStatus === "running"
              ? "border-indigo-500 bg-indigo-500/10 shadow-lg shadow-indigo-500/20"
              : orchestratorStatus === "completed"
                ? "border-green-500/50 bg-green-500/5"
                : "border-[var(--border-secondary)] bg-[var(--bg-elevated)]"
          )}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-12 h-12 rounded-xl flex items-center justify-center text-2xl"
              style={{ backgroundColor: `${orchestratorInfo.color}20` }}
            >
              {orchestratorInfo.icon}
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-bold text-[var(--text-primary)]">{orchestratorInfo.label}</h3>
                <StatusDot status={orchestratorStatus} />
              </div>
              <p className="text-[10px] text-[var(--text-muted)]">Manages execution workflow (code &rarr; test &rarr; fix &rarr; deploy)</p>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-400 font-medium">
                  {orchestratorInfo.provider}
                </span>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-300">
                  COORDINATOR
                </span>
                {currentPhase && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">
                    Phase: {currentPhase}
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Fan-out connectors */}
        <div className="relative w-full max-w-3xl mt-2">
          <div className="flex justify-center">
            <div className="w-0.5 h-4 bg-indigo-500/30" />
          </div>
          <div className="h-0.5 bg-gradient-to-r from-transparent via-indigo-500/30 to-transparent mx-8" />
        </div>
      </div>

      {/* Execution Phases (specialist agents grouped) */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3 mt-2">
        {EXECUTION_PHASES.map((phase) => {
          const phaseAgents = phase.agents;
          const allDone = phaseAgents.every((a) => agentStatuses[a]?.status === "completed");
          const anyRunning = phaseAgents.some((a) => agentStatuses[a]?.status === "running");
          const anyFailed = phaseAgents.some((a) => agentStatuses[a]?.status === "failed");
          const isActive = currentPhase === phase.name;

          return (
            <div
              key={phase.name}
              className={clsx(
                "rounded-xl border p-3 transition-all",
                isActive
                  ? "border-blue-500/50 bg-blue-500/5 shadow-sm shadow-blue-500/10"
                  : allDone
                    ? "border-green-500/30 bg-green-500/5"
                    : anyFailed
                      ? "border-red-500/30 bg-red-500/5"
                      : "border-[var(--border-primary)] bg-[var(--bg-secondary)]"
              )}
            >
              {/* Phase header */}
              <div className="flex items-center gap-2 mb-2.5">
                <div className="w-2 h-2 rounded-full" style={{ backgroundColor: phase.color }} />
                <h4
                  className="text-[11px] font-bold uppercase tracking-wider"
                  style={{ color: phase.color }}
                >
                  {phase.name}
                </h4>
                {allDone && <span className="text-[9px] text-green-400 ml-auto">Done</span>}
                {anyRunning && <span className="text-[9px] text-blue-400 ml-auto animate-pulse">Active</span>}
              </div>

              {/* Agents in this phase */}
              <div className="space-y-1.5">
                {phaseAgents.map((agentKey) => {
                  const info = AGENT_INFO[agentKey];
                  if (!info) return null;
                  const status = agentStatuses[agentKey]?.status;
                  const msgCount = a2aMessages.filter(
                    (m) => m.from_agent === agentKey || m.to_agent === agentKey
                  ).length;

                  return (
                    <div
                      key={agentKey}
                      className={clsx(
                        "flex items-center gap-2 px-2 py-1.5 rounded-lg transition-colors",
                        status === "running"
                          ? "bg-blue-500/10"
                          : status === "completed"
                            ? "bg-green-500/5"
                            : status === "failed"
                              ? "bg-red-500/5"
                              : "bg-[var(--bg-tertiary)]/50"
                      )}
                    >
                      <span className="text-sm">{info.icon}</span>
                      <div className="flex-1 min-w-0">
                        <span className="text-[10px] font-medium text-[var(--text-primary)] truncate block">
                          {info.label}
                        </span>
                        <span className="text-[8px] text-[var(--text-muted)]">{info.provider}</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        {msgCount > 0 && (
                          <span className="text-[8px] px-1 py-0.5 rounded bg-blue-500/15 text-blue-400">
                            {msgCount} msg
                          </span>
                        )}
                        <StatusDot status={status} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
