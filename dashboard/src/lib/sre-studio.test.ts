import assert from "node:assert/strict";
import test from "node:test";

import {
  filterStudioDrafts,
  formatDuration,
  persistStudioDraft,
  summarizeStageEvents,
} from "./sre-studio.ts";

const drafts = [
  { id: "1", name: "Cluster health sweep", kind: "blueprint", status: "draft" },
  { id: "2", name: "Latency investigator", kind: "agent", status: "published" },
  { id: "3", name: "Release guard", kind: "blueprint", status: "published" },
];

test("filters studio drafts by name, kind, or status without case sensitivity", () => {
  assert.deepEqual(filterStudioDrafts(drafts, "LATENCY"), [drafts[1]]);
  assert.deepEqual(filterStudioDrafts(drafts, "agent"), [drafts[1]]);
  assert.deepEqual(filterStudioDrafts(drafts, "published"), [drafts[1], drafts[2]]);
  assert.equal(filterStudioDrafts(drafts, "  "), drafts);
});

test("formats dry-run durations for scan-friendly summaries", () => {
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(480), "480ms");
  assert.equal(formatDuration(1_250), "1.3s");
  assert.equal(formatDuration(65_000), "1m 5s");
});

test("summarizes terminal stage events and ignores transient started events", () => {
  assert.deepEqual(
    summarizeStageEvents([
      { stage: "discover", phase: "started" },
      { stage: "discover", phase: "completed", duration_ms: 1_200 },
      { stage: "inspect", phase: "failed", duration_ms: 800 },
      { stage: "respond", phase: "skipped" },
    ]),
    {
      events: [
        { stage: "discover", phase: "completed", duration_ms: 1_200 },
        { stage: "inspect", phase: "failed", duration_ms: 800 },
        { stage: "respond", phase: "skipped" },
      ],
      completed: 1,
      failed: 1,
      skipped: 1,
      totalDurationMs: 2_000,
    },
  );
});

test("reports unknown duration when stage events contain no timing data", () => {
  assert.equal(summarizeStageEvents([{ stage: "respond", phase: "skipped" }]).totalDurationMs, null);
});

test("persists changed YAML and returns the saved draft for the UI", async () => {
  const draft = { id: "draft-1", yaml: "name: old", status: "draft" };
  const calls: Array<{ id: string; yaml: string }> = [];

  const saved = await persistStudioDraft(draft, "name: new", async (id, input) => {
    calls.push({ id, yaml: input.yaml });
    return { ...draft, yaml: input.yaml };
  });

  assert.deepEqual(calls, [{ id: "draft-1", yaml: "name: new" }]);
  assert.deepEqual(saved, { id: "draft-1", yaml: "name: new", status: "draft" });
});

test("does not send unchanged YAML back to the server", async () => {
  const draft = { id: "draft-1", yaml: "name: current" };
  let updateCalled = false;

  const saved = await persistStudioDraft(draft, draft.yaml, async () => {
    updateCalled = true;
    return draft;
  });

  assert.equal(saved, draft);
  assert.equal(updateCalled, false);
});
