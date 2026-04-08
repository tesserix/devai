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
      <div className="flex items-center gap-0.5 min-w-[1000px] px-2 py-4">
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
              <div className="flex flex-col items-center gap-1">
                <div
                  className={clsx(
                    "flex items-center justify-center text-xs font-semibold border-2 transition-all",
                    isCoordinator ? "w-9 h-9 rounded-xl" : "w-8 h-8 rounded-full",
                    isDone && "bg-green-50 dark:bg-green-950 border-green-500 dark:border-green-600 text-green-700 dark:text-green-400",
                    isActive && "bg-indigo-50 dark:bg-indigo-950 border-indigo-500 text-indigo-700 dark:text-indigo-400 ring-2 ring-indigo-200 dark:ring-indigo-900",
                    isFailed && "bg-red-50 dark:bg-red-950 border-red-500 text-red-700 dark:text-red-400",
                    !isDone && !isActive && !isFailed && "bg-gray-100 dark:bg-gray-700 border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500",
                    isCoordinator && isActive && "border-indigo-600 bg-indigo-50 dark:bg-indigo-950 ring-indigo-200 dark:ring-indigo-900",
                    isCoordinator && isDone && "border-green-500 dark:border-green-600",
                  )}
                >
                  {isDone ? (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="20 6 9 17 4 12"/>
                    </svg>
                  ) : (
                    <span>{idx + 1}</span>
                  )}
                </div>
                <span
                  className={clsx(
                    "text-xs font-medium whitespace-nowrap",
                    isDone && "text-green-600 dark:text-green-500",
                    isActive && "text-indigo-600 dark:text-indigo-400",
                    !isDone && !isActive && "text-gray-400 dark:text-gray-500"
                  )}
                >
                  {stage.label}
                </span>
                {agent && (
                  <span
                    className={clsx(
                      "text-xs whitespace-nowrap",
                      isCoordinator
                        ? "text-indigo-500 dark:text-indigo-400 font-medium"
                        : "text-gray-400 dark:text-gray-500"
                    )}
                  >
                    {stage.key === "triggered" ? "Coord" : agent.provider}
                  </span>
                )}
                {timing !== undefined && (
                  <span className="text-xs text-gray-400 dark:text-gray-500">
                    {timing.toFixed(1)}s
                  </span>
                )}
              </div>

              {/* Connector */}
              {idx < stages.length - 1 && (
                <div
                  className={clsx(
                    "w-6 h-px mx-0.5",
                    currentIdx >= 0 && idx < currentIdx
                      ? "bg-green-400 dark:bg-green-600"
                      : "bg-gray-200 dark:bg-gray-600"
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
