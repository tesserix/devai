import assert from "node:assert/strict";
import test from "node:test";

import { buildQueuedRunView, formatQueueElapsed } from "./queued-run.ts";

test("queued elapsed time remains reassuring and human readable", () => {
  assert.equal(formatQueueElapsed(0), "just now");
  assert.equal(formatQueueElapsed(42), "42 seconds");
  assert.equal(formatQueueElapsed(61), "1 minute 1 second");
  assert.equal(formatQueueElapsed(3_661), "1 hour 1 minute");
});

test("queued view uses durable timestamps and real lifecycle events", () => {
  const view = buildQueuedRunView(
    {
      created_at: 1_000,
      updated_at: 1_045,
      stage_events: [
        { stage: "hydrate-context", phase: "started", message: "Loading repository context", timestamp: 1_042 },
      ],
    },
    1_070_000,
  );

  assert.equal(view.elapsedLabel, "1 minute 10 seconds");
  assert.equal(view.safeToLeave, true);
  assert.equal(view.updatedAtMs, 1_045_000);
  assert.deepEqual(view.recentEvents, [
    {
      label: "hydrate context · started",
      detail: "Loading repository context",
      timestampMs: 1_042_000,
    },
  ]);
});

test("queued view falls back to the accepted event without inventing progress", () => {
  const view = buildQueuedRunView(
    { created_at: "2026-08-27T05:48:25Z", events: [] },
    Date.parse("2026-08-27T05:48:35Z"),
  );

  assert.equal(view.elapsedLabel, "10 seconds");
  assert.equal(view.safeToLeave, false);
  assert.deepEqual(view.recentEvents, [
    {
      label: "Run accepted",
      detail: "DevAI saved the run and is waiting for an available worker.",
      timestampMs: Date.parse("2026-08-27T05:48:25Z"),
    },
  ]);
});
