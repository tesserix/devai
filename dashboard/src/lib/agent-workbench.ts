import { sandboxPath } from "./api.ts";

type AgentSandbox = {
  id: string;
  spec: { agent: { name: string; version: string } };
};

export function failedEvaluationWorkspacePath(passed: boolean, sandboxId: string): string | null {
  return passed ? null : sandboxPath(sandboxId);
}

type CostedInvocation = {
  id: string;
  totals: { cost_usd: number };
};

type CostedEvaluation = {
  results: Array<{ invocation_id?: string }>;
  summary: { cost_usd: number };
};

export function agentSandboxes<T extends AgentSandbox>(sandboxes: T[], agentName: string): T[] {
  return sandboxes.filter((sandbox) => sandbox.spec.agent.name === agentName);
}

export function sandboxRemainingSeconds(expiresAt: string, now = new Date()): number {
  return Math.max(0, Math.floor((new Date(expiresAt).getTime() - now.getTime()) / 1000));
}

export function comparisonDeltaTone(
  metric: string,
  delta: number,
): "improved" | "regressed" | "unchanged" {
  if (delta === 0) return "unchanged";
  const lowerIsBetter = ["cost", "latency", "duration", "token"].some((word) => metric.includes(word));
  const improved = lowerIsBetter ? delta < 0 : delta > 0;
  return improved ? "improved" : "regressed";
}

export function sandboxBudget(
  limitUsd: number,
  invocations: CostedInvocation[],
  evaluations: CostedEvaluation[],
): { spentUsd: number; remainingUsd: number; limitUsd: number } {
  const evaluatedInvocations = new Set(
    evaluations.flatMap((evaluation) => evaluation.results.map((result) => result.invocation_id).filter(Boolean)),
  );
  const invocationCost = invocations.reduce(
    (total, invocation) => total + (evaluatedInvocations.has(invocation.id) ? 0 : invocation.totals.cost_usd),
    0,
  );
  const evaluationCost = evaluations.reduce((total, evaluation) => total + evaluation.summary.cost_usd, 0);
  const spentUsd = invocationCost + evaluationCost;
  return {
    spentUsd,
    remainingUsd: Math.max(0, limitUsd - spentUsd),
    limitUsd,
  };
}
