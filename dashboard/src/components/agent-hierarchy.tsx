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
      ? "bg-green-600"
      : status === "running"
        ? "bg-blue-500 animate-pulse"
        : status === "failed"
          ? "bg-red-600"
          : status === "waiting_approval"
            ? "bg-amber-500 animate-pulse"
            : "bg-gray-300 dark:bg-slate-600";

  return <span className={clsx("w-2 h-2 rounded-full inline-block shrink-0", color)} />;
}

export function AgentHierarchy({ agentStatuses, a2aMessages, orchestratorRouting }: AgentHierarchyProps) {
  const supervisorInfo = AGENT_INFO["supervisor"];
  const orchestratorInfo = AGENT_INFO["orchestrator"];
  const supervisorStatus = agentStatuses["supervisor"]?.status;
  const orchestratorStatus = agentStatuses["orchestrator"]?.status;

  const progressPct = orchestratorRouting?.progress_pct ?? 0;
  const currentPhase = orchestratorRouting?.current_phase ?? "";

  return (
    <div className="space-y-5">
      {/* Progress Bar */}
      <div className="p-4 rounded-xl border border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-gray-500 dark:text-slate-400 uppercase tracking-wider">
            Pipeline Progress
          </span>
          <span className="text-xs font-mono text-gray-500 dark:text-slate-400">{progressPct}%</span>
        </div>
        <div className="w-full h-1.5 rounded-full bg-gray-100 dark:bg-slate-800 overflow-hidden">
          <div
            className="h-full rounded-full bg-indigo-600 transition-all duration-700"
            style={{ width: `${progressPct}%` }}
          />
        </div>
        {orchestratorRouting?.status_summary && (
          <p className="text-[11px] text-gray-400 dark:text-slate-500 mt-2">{orchestratorRouting.status_summary}</p>
        )}
      </div>

      {/* Supervisor Node */}
      <div className="flex flex-col items-center">
        <div
          className={clsx(
            "relative w-full max-w-md p-4 rounded-xl border-2 transition-all",
            supervisorStatus === "running"
              ? "border-indigo-400 dark:border-indigo-600 bg-indigo-50 dark:bg-indigo-950/40"
              : supervisorStatus === "completed"
                ? "border-green-300 dark:border-green-800 bg-green-50 dark:bg-green-950/20"
                : "border-gray-200 dark:border-slate-700 bg-white dark:bg-slate-900"
          )}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-11 h-11 rounded-xl flex items-center justify-center text-sm font-bold shrink-0"
              style={{ backgroundColor: `${supervisorInfo.color}18`, color: supervisorInfo.color }}
            >
              S
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-slate-100">{supervisorInfo.label}</h3>
                <StatusDot status={supervisorStatus} />
              </div>
              <p className="text-[11px] text-gray-400 dark:text-slate-500">Plans architecture &amp; delegates tasks</p>
              <div className="flex items-center gap-2 mt-1.5">
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 font-medium border border-indigo-100 dark:border-indigo-900">
                  {supervisorInfo.provider}
                </span>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-slate-800 text-gray-500 dark:text-slate-400">
                  COORDINATOR
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Connector */}
        <div className="w-px h-5 bg-gray-200 dark:bg-slate-700" />
        <div className="w-2 h-2 rotate-45 border-b border-r border-gray-300 dark:border-slate-600 -mt-1.5" />
      </div>

      {/* Orchestrator Node */}
      <div className="flex flex-col items-center -mt-1">
        <div
          className={clsx(
            "relative w-full max-w-md p-4 rounded-xl border-2 transition-all",
            orchestratorStatus === "running"
              ? "border-indigo-400 dark:border-indigo-600 bg-indigo-50 dark:bg-indigo-950/40"
              : orchestratorStatus === "completed"
                ? "border-green-300 dark:border-green-800 bg-green-50 dark:bg-green-950/20"
                : "border-gray-200 dark:border-slate-700 bg-white dark:bg-slate-900"
          )}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-11 h-11 rounded-xl flex items-center justify-center text-sm font-bold shrink-0"
              style={{ backgroundColor: `${orchestratorInfo.color}18`, color: orchestratorInfo.color }}
            >
              O
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-gray-900 dark:text-slate-100">{orchestratorInfo.label}</h3>
                <StatusDot status={orchestratorStatus} />
              </div>
              <p className="text-[11px] text-gray-400 dark:text-slate-500">Manages execution workflow (code &rarr; test &rarr; fix &rarr; deploy)</p>
              <div className="flex items-center gap-2 mt-1.5">
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 font-medium border border-indigo-100 dark:border-indigo-900">
                  {orchestratorInfo.provider}
                </span>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-slate-800 text-gray-500 dark:text-slate-400">
                  COORDINATOR
                </span>
                {currentPhase && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400 border border-blue-100 dark:border-blue-900">
                    Phase: {currentPhase}
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Fan-out connector */}
        <div className="w-px h-4 bg-gray-200 dark:bg-slate-700 mt-0.5" />
        <div className="w-full max-w-2xl h-px bg-gray-200 dark:bg-slate-700" />
      </div>

      {/* Execution Phases */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
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
                  ? "border-indigo-200 dark:border-indigo-800 bg-indigo-50 dark:bg-indigo-950/30"
                  : allDone
                    ? "border-green-200 dark:border-green-900 bg-green-50 dark:bg-green-950/20"
                    : anyFailed
                      ? "border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/20"
                      : "border-gray-200 dark:border-slate-800 bg-white dark:bg-slate-900"
              )}
            >
              {/* Phase header */}
              <div className="flex items-center gap-2 mb-2.5">
                <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: phase.color }} />
                <h4 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: phase.color }}>
                  {phase.name}
                </h4>
                {allDone && <span className="text-[9px] text-green-600 dark:text-green-500 ml-auto">Done</span>}
                {anyRunning && <span className="text-[9px] text-blue-600 dark:text-blue-400 ml-auto animate-pulse">Active</span>}
              </div>

              {/* Agents in this phase */}
              <div className="space-y-1">
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
                          ? "bg-blue-50 dark:bg-blue-950/30"
                          : status === "completed"
                            ? "bg-green-50 dark:bg-green-950/20"
                            : status === "failed"
                              ? "bg-red-50 dark:bg-red-950/20"
                              : "bg-gray-50 dark:bg-slate-800/50"
                      )}
                    >
                      <div
                        className="w-5 h-5 rounded flex items-center justify-center text-[9px] font-semibold shrink-0"
                        style={{ backgroundColor: `${info.color}18`, color: info.color }}
                      >
                        {info.label.charAt(0)}
                      </div>
                      <div className="flex-1 min-w-0">
                        <span className="text-[10px] font-medium text-gray-800 dark:text-slate-200 truncate block">
                          {info.label}
                        </span>
                        <span className="text-[8px] text-gray-400 dark:text-slate-500">{info.provider}</span>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {msgCount > 0 && (
                          <span className="text-[8px] px-1 py-0.5 rounded bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400">
                            {msgCount}
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
