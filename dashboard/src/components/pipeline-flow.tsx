"use client";

import { clsx } from "clsx";
import { PIPELINE_STAGES, AGENT_INFO, STAGE_TO_AGENT } from "@/lib/constants";

interface PipelineFlowProps {
  currentStage: string;
  agentTimings?: Record<string, number>;
}

export function PipelineFlow({ currentStage, agentTimings = {} }: PipelineFlowProps) {
  const stages = PIPELINE_STAGES.filter((s) => s.key !== "failed");
  const currentIdx = stages.findIndex((s) => s.key === currentStage);

  return (
    <div className="w-full overflow-x-auto">
      <div className="flex items-center gap-1 min-w-[1000px] px-2 py-4">
        {stages.map((stage, idx) => {
          const isActive = stage.key === currentStage;
          const isDone = currentIdx >= 0 && idx < currentIdx;
          const isFailed = currentStage === "failed" && idx === currentIdx;
          const agentKey = STAGE_TO_AGENT[stage.key];
          const agent = agentKey ? AGENT_INFO[agentKey] : null;
          const timing = agentKey ? agentTimings[agentKey] : undefined;
          const isCoordinator = agent?.role === "coordinator";

          return (
            <div key={stage.key} className="flex items-center">
              {/* Node */}
              <div className="flex flex-col items-center gap-1.5">
                <div
                  className={clsx(
                    "flex items-center justify-center text-sm font-medium border-2 transition-all",
                    isCoordinator ? "w-10 h-10 rounded-xl" : "w-9 h-9 rounded-full",
                    isDone && "bg-green-500/20 border-green-500 text-green-400",
                    isActive && "bg-blue-500/20 border-blue-500 text-blue-400 ring-2 ring-blue-500/30",
                    isFailed && "bg-red-500/20 border-red-500 text-red-400",
                    !isDone && !isActive && !isFailed && "bg-[var(--bg-tertiary)] border-[var(--border-primary)] text-[var(--text-muted)]",
                    isCoordinator && isActive && "border-violet-500 bg-violet-500/20 text-violet-400 ring-violet-500/30",
                    isCoordinator && isDone && "border-violet-400/50 bg-violet-500/10 text-violet-400"
                  )}
                >
                  {isDone ? "✓" : isActive ? (agent?.icon || "●") : idx + 1}
                </div>
                <span
                  className={clsx(
                    "text-[10px] font-medium whitespace-nowrap",
                    isDone && "text-green-400",
                    isActive && (isCoordinator ? "text-violet-400" : "text-blue-400"),
                    !isDone && !isActive && "text-[var(--text-muted)]"
                  )}
                >
                  {stage.label}
                </span>
                {agent && (
                  <span
                    className={clsx(
                      "text-[9px] whitespace-nowrap",
                      isCoordinator ? "text-violet-400/70 font-medium" : "text-[var(--text-muted)]"
                    )}
                  >
                    {isCoordinator ? "Coordinator" : agent.provider}
                  </span>
                )}
                {timing !== undefined && (
                  <span className="text-[9px] text-[var(--text-secondary)]">
                    {timing.toFixed(1)}s
                  </span>
                )}
              </div>

              {/* Connector */}
              {idx < stages.length - 1 && (
                <div
                  className={clsx(
                    "w-8 h-0.5 mx-0.5",
                    currentIdx >= 0 && idx < currentIdx ? "bg-green-500/50" : "bg-[var(--border-primary)]"
                  )}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
