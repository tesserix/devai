"use client";

import { clsx } from "clsx";
import { PIPELINE_STAGES, AGENT_INFO, STAGE_TO_AGENT } from "@/lib/constants";

interface PipelineFlowProps {
  currentStage: string;
  agentTimings?: Record<string, number>;
}

export function PipelineFlow({ currentStage, agentTimings = {} }: PipelineFlowProps) {
  const currentIdx = PIPELINE_STAGES.findIndex((s) => s.key === currentStage);

  return (
    <div className="w-full overflow-x-auto">
      <div className="flex items-center gap-1 min-w-[900px] px-2 py-4">
        {PIPELINE_STAGES.filter((s) => s.key !== "failed").map((stage, idx) => {
          const isActive = stage.key === currentStage;
          const isDone = idx < currentIdx;
          const isFailed = currentStage === "failed" && idx === currentIdx;
          const agentKey = STAGE_TO_AGENT[stage.key];
          const agent = agentKey ? AGENT_INFO[agentKey] : null;
          const timing = agentKey ? agentTimings[agentKey] : undefined;

          return (
            <div key={stage.key} className="flex items-center">
              {/* Node */}
              <div className="flex flex-col items-center gap-1.5">
                <div
                  className={clsx(
                    "w-9 h-9 rounded-full flex items-center justify-center text-sm font-medium border-2 transition-all",
                    isDone && "bg-green-500/20 border-green-500 text-green-400",
                    isActive && "bg-blue-500/20 border-blue-500 text-blue-400 ring-2 ring-blue-500/30",
                    isFailed && "bg-red-500/20 border-red-500 text-red-400",
                    !isDone && !isActive && !isFailed && "bg-[var(--bg-tertiary)] border-[var(--border-primary)] text-[var(--text-muted)]"
                  )}
                >
                  {isDone ? "✓" : isActive ? "●" : idx + 1}
                </div>
                <span
                  className={clsx(
                    "text-[10px] font-medium whitespace-nowrap",
                    isDone && "text-green-400",
                    isActive && "text-blue-400",
                    !isDone && !isActive && "text-[var(--text-muted)]"
                  )}
                >
                  {stage.label}
                </span>
                {agent && (
                  <span className="text-[9px] text-[var(--text-muted)] whitespace-nowrap">
                    {agent.provider}
                  </span>
                )}
                {timing !== undefined && (
                  <span className="text-[9px] text-[var(--text-secondary)]">
                    {timing.toFixed(1)}s
                  </span>
                )}
              </div>

              {/* Connector */}
              {idx < PIPELINE_STAGES.length - 2 && (
                <div
                  className={clsx(
                    "w-8 h-0.5 mx-0.5",
                    idx < currentIdx ? "bg-green-500/50" : "bg-[var(--border-primary)]"
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
