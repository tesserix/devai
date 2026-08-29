import assert from "node:assert/strict";
import test from "node:test";

import {
  agentSandboxes,
  comparisonDeltaTone,
  failedEvaluationWorkspacePath,
  sandboxBudget,
  sandboxRemainingSeconds,
} from "./agent-workbench.ts";

test("only failed evaluation cases link to their encoded workspace", () => {
  assert.equal(failedEvaluationWorkspacePath(false, "sandbox /?#"), "/sandboxes/sandbox%20%2F%3F%23");
  assert.equal(failedEvaluationWorkspacePath(true, "sandbox-id"), null);
});

test("agent workbench includes only sandboxes for the selected agent", () => {
  assert.deepEqual(
    agentSandboxes(
      [
        { id: "candidate", spec: { agent: { name: "support", version: "15" } } },
        { id: "foreign", spec: { agent: { name: "billing", version: "8" } } },
        { id: "production", spec: { agent: { name: "support", version: "14" } } },
      ],
      "support",
    ).map((sandbox) => sandbox.id),
    ["candidate", "production"],
  );
});

test("comparison delta direction distinguishes quality from cost and latency", () => {
  assert.equal(comparisonDeltaTone("pass_rate", 0.1), "improved");
  assert.equal(comparisonDeltaTone("cost_usd", -0.1), "improved");
  assert.equal(comparisonDeltaTone("p95_latency_ms", 50), "regressed");
  assert.equal(comparisonDeltaTone("tokens", 0), "unchanged");
});

test("sandbox remaining time is clamped after expiry", () => {
  assert.equal(
    sandboxRemainingSeconds("2026-08-19T12:30:00Z", new Date("2026-08-19T12:29:00Z")),
    60,
  );
  assert.equal(
    sandboxRemainingSeconds("2026-08-19T12:30:00Z", new Date("2026-08-19T12:31:00Z")),
    0,
  );
});

test("sandbox budget reports invocation and evaluation spend without double counting", () => {
  assert.deepEqual(
    sandboxBudget(
      10,
      [
        { id: "playground", totals: { cost_usd: 0.125 } },
        { id: "eval-case", totals: { cost_usd: 0.375 } },
      ],
      [{ results: [{ invocation_id: "eval-case" }], summary: { cost_usd: 1.25 } }],
    ),
    { spentUsd: 1.375, remainingUsd: 8.625, limitUsd: 10 },
  );
});
