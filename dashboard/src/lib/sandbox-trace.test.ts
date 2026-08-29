import assert from "node:assert/strict";
import test from "node:test";

import { traceLatencyMs, traceStepBadges } from "./sandbox-trace.ts";

test("trace step badges expose provider prompt version and per-call cost", () => {
  assert.deepEqual(
    traceStepBadges({
      provider: "anthropic",
      prompt_version: "v7",
      cost_usd: 0.001234,
    }),
    ["anthropic", "prompt v7", "$0.001234"],
  );
});

test("trace latency prefers wall clock and supports legacy traces", () => {
  assert.equal(traceLatencyMs({ wall_clock_ms: 87, latency_ms: 140 }), 87);
  assert.equal(traceLatencyMs({ latency_ms: 140 }), 140);
});
