export const AGENT_LIFECYCLE_STAGES = ["authored", "tested", "gated", "published", "running"] as const;

export type AgentLifecycleStage = (typeof AGENT_LIFECYCLE_STAGES)[number];

export type AgentGateStatus = "passed" | "blocked" | "overridden" | "";

export type AgentGateResult = {
  status?: AgentGateStatus;
  candidate_run_id?: string;
  failing_cases?: string[];
  failing_thresholds?: Record<string, string>;
  issues?: string[];
};

export function agentLifecycle(input: {
  evaluationRunId?: string;
  gateStatus?: AgentGateStatus;
  published?: boolean;
  running?: boolean;
}): { current: AgentLifecycleStage; completed: AgentLifecycleStage[] } {
  const completed: AgentLifecycleStage[] = ["authored"];
  const gated = input.gateStatus === "passed" || input.gateStatus === "overridden";
  if (input.evaluationRunId || gated) completed.push("tested");
  if (gated) completed.push("gated");
  if (input.published) completed.push("published");
  if (input.running) completed.push("running");
  return { current: completed.at(-1) ?? "authored", completed };
}

export function gateFailureMessages(gate: AgentGateResult | null | undefined): string[] {
  if (!gate) return [];
  return [
    ...(gate.issues ?? []),
    ...(gate.failing_cases ?? []).map((name) => `Case ${name} failed`),
    ...Object.entries(gate.failing_thresholds ?? {}).map(([name, detail]) => `${name}: ${detail}`),
  ];
}
