import assert from "node:assert/strict";
import test from "node:test";

import {
  agentLifecycle,
  gateAllowsAdminOverride,
  gateFailureMessages,
  lifecycleGateFromError,
} from "./agent-lifecycle.ts";

test("agent lifecycle advances only from durable server evidence", () => {
  assert.deepEqual(agentLifecycle({}), {
    current: "authored",
    completed: ["authored"],
  });
  assert.deepEqual(agentLifecycle({ evaluationRunId: "eval-1" }), {
    current: "tested",
    completed: ["authored", "tested"],
  });
  assert.deepEqual(
    agentLifecycle({ evaluationRunId: "eval-1", gateStatus: "passed" }),
    {
      current: "gated",
      completed: ["authored", "tested", "gated"],
    },
  );
  assert.deepEqual(
    agentLifecycle({ gateStatus: "overridden", published: true }),
    {
      current: "published",
      completed: ["authored", "tested", "gated", "published"],
    },
  );
  assert.deepEqual(agentLifecycle({ published: true }), {
    current: "published",
    completed: ["authored", "published"],
  });
  assert.deepEqual(
    agentLifecycle({ evaluationRunId: "eval-1", gateStatus: "passed", published: true, running: true }),
    {
    current: "running",
    completed: ["authored", "tested", "gated", "published", "running"],
    },
  );
});

test("blocked gates retain tested state and surface exact failures", () => {
  assert.equal(
    agentLifecycle({ evaluationRunId: "eval-1", gateStatus: "blocked" }).current,
    "tested",
  );
  assert.deepEqual(
    gateFailureMessages({
      failing_cases: ["refund-policy"],
      failing_thresholds: {
        success: "actual 0.8; required at least 0.95",
      },
      issues: ["evaluation run does not match the agent draft"],
    }),
    [
      "evaluation run does not match the agent draft",
      "Case refund-policy failed",
      "success: actual 0.8; required at least 0.95",
    ],
  );
});

test("extracts both evaluation and static lifecycle gate failures", () => {
  const staticGate = { status: "blocked" as const, issues: ["missing skill"] };
  assert.deepEqual(
    lifecycleGateFromError({
      detail: { code: "agent_lifecycle_gate_blocked", gate: staticGate },
    }),
    staticGate,
  );
  assert.equal(lifecycleGateFromError({ detail: { code: "another_error", gate: staticGate } }), null);
});

test("allows overrides only for evaluation failures or approval holds", () => {
  assert.equal(gateAllowsAdminOverride({ status: "blocked", failing_cases: ["case-1"] }), true);
  assert.equal(
    gateAllowsAdminOverride({
      status: "blocked",
      stages: [{ name: "security", status: "blocked", issues: ["prompt injection"] }],
    }),
    false,
  );
  assert.equal(gateAllowsAdminOverride({ status: "approval_required", requires_approval: true }), true);
});
