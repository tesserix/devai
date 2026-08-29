import test from "node:test";
import assert from "node:assert/strict";

import { DEMO_IDEAS, shouldShowOnboarding } from "./demo-ideas.ts";

const fresh = { trial_enabled: true, applicable: true, budget: 100, used: 0, remaining: 100, exhausted: false, warning: false };
const spent = { trial_enabled: true, applicable: true, budget: 100, used: 100, remaining: 0, exhausted: true, warning: true };

test("every demo idea has a title, blurb and href", () => {
  assert.ok(DEMO_IDEAS.length >= 3);
  for (const idea of DEMO_IDEAS) {
    assert.ok(idea.title.length > 0);
    assert.ok(idea.blurb.length > 0);
    assert.ok(idea.href.startsWith("/"));
  }
});

test("onboarding shows when the trial is fresh and has not been seen", () => {
  assert.equal(shouldShowOnboarding(false, fresh), true);
});

test("onboarding does not show once dismissed", () => {
  assert.equal(shouldShowOnboarding(true, fresh), false);
});

test("onboarding does not show once the trial is exhausted", () => {
  assert.equal(shouldShowOnboarding(false, spent), false);
});

test("onboarding does not show when the trial is disabled", () => {
  assert.equal(
    shouldShowOnboarding(false, { trial_enabled: false, budget: 0, used: 0, remaining: 0, exhausted: false, warning: false }),
    false
  );
});

test("onboarding does not show when the trial does not apply to this user", () => {
  assert.equal(shouldShowOnboarding(false, { ...fresh, applicable: false }), false);
});
