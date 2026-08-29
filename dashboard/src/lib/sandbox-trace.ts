type TraceStepAttribution = {
  provider?: string;
  prompt_version?: string;
  cost_usd: number;
};

type TraceLatency = {
  latency_ms: number;
  wall_clock_ms?: number;
};

export function traceStepBadges(step: TraceStepAttribution): string[] {
  const badges: string[] = [];
  if (step.provider) badges.push(step.provider);
  if (step.prompt_version) badges.push(`prompt ${step.prompt_version}`);
  if (step.cost_usd > 0) badges.push(`$${step.cost_usd.toFixed(6)}`);
  return badges;
}

export function traceLatencyMs(totals: TraceLatency): number {
  return totals.wall_clock_ms ?? totals.latency_ms;
}
