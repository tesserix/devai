import assert from "node:assert/strict";
import test from "node:test";
import { formatEvalCost } from "./eval-cost.ts";

test("eval cost names the agent, judge, and infrastructure attribution", () => {
  assert.equal(
    formatEvalCost({
      cost_usd: 0.012345,
      cost_breakdown: {
        agent_cost_usd: 0.01,
        judge_cost_usd: 0.002,
        infrastructure_cost_usd: 0.000345,
      },
    }),
    "$0.012345 total · agent $0.010000 · judge $0.002000 · infrastructure $0.000345",
  );
});
