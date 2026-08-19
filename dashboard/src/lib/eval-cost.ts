export type EvalCost = {
  cost_usd: number;
  cost_breakdown?: {
    agent_cost_usd: number;
    judge_cost_usd: number;
    infrastructure_cost_usd: number;
  };
};

function usd(value: number): string {
  return `$${value.toFixed(6)}`;
}

export function formatEvalCost(summary: EvalCost): string {
  const breakdown = summary.cost_breakdown ?? {
    agent_cost_usd: summary.cost_usd,
    judge_cost_usd: 0,
    infrastructure_cost_usd: 0,
  };
  return `${usd(summary.cost_usd)} total · agent ${usd(breakdown.agent_cost_usd)} · judge ${usd(breakdown.judge_cost_usd)} · infrastructure ${usd(breakdown.infrastructure_cost_usd)}`;
}
