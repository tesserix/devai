export const AGENT_LIFECYCLE_STAGES = ["authored", "tested", "gated", "published", "running"] as const;

export type AgentLifecycleStage = (typeof AGENT_LIFECYCLE_STAGES)[number];

export type AgentGateStatus = "passed" | "blocked" | "overridden" | "approval_required" | "";

export type AgentGateResult = {
  status?: AgentGateStatus;
  candidate_run_id?: string;
  failing_cases?: string[];
  failing_thresholds?: Record<string, string>;
  issues?: string[];
  requires_approval?: boolean;
  stages?: Array<{
    name: "build" | "security";
    status: "passed" | "blocked" | "approval_required";
    issues?: string[];
  }>;
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

export function lifecycleGateFromError(body: unknown): AgentGateResult | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const typed = detail as { code?: unknown; gate?: unknown };
  if (
    typed.code !== "agent_evaluation_gate_blocked" &&
    typed.code !== "agent_lifecycle_gate_blocked"
  ) {
    return null;
  }
  return typed.gate && typeof typed.gate === "object" ? (typed.gate as AgentGateResult) : null;
}

export function gateAllowsAdminOverride(gate: AgentGateResult): boolean {
  if (gate.status === "approval_required") return true;
  return gate.status === "blocked" && !gate.stages;
}
