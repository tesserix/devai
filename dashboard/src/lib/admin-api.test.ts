import test from "node:test";
import assert from "node:assert/strict";

import { adminOverviewPath, adminOpenPanelPath, trialTone } from "./admin-api.ts";

test("overview path carries the day window", () => {
  assert.equal(adminOverviewPath(7), "/admin/overview?days=7");
});

test("openpanel path carries the day window", () => {
  assert.equal(adminOpenPanelPath(30), "/admin/openpanel?days=30");
});

test("trial tone is ok below the warning threshold", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 10, remaining: 90, exhausted: false, warning: false }), "ok");
});

test("trial tone warns at the 80 percent mark", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 80, remaining: 20, exhausted: false, warning: true }), "warning");
});

test("trial tone is exhausted when the budget is spent", () => {
  assert.equal(trialTone({ trial_enabled: true, budget: 100, used: 100, remaining: 0, exhausted: true, warning: true }), "exhausted");
});

test("trial tone is hidden when trials are disabled", () => {
  assert.equal(trialTone({ trial_enabled: false, budget: 0, used: 0, remaining: 0, exhausted: false, warning: false }), "hidden");
});
