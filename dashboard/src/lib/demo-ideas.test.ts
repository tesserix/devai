import test from "node:test";
import assert from "node:assert/strict";

import { DEMO_IDEAS, shouldShowOnboarding, trialSurface } from "./demo-ideas.ts";

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

const warningLevel = { trial_enabled: true, applicable: true, budget: 100, used: 85, remaining: 15, exhausted: false, warning: true };

test("trial surface is meter at warning level even when onboarding has not been seen", () => {
  assert.equal(trialSurface(false, warningLevel), "meter");
});

test("trial surface is meter at warning level once seen", () => {
  assert.equal(trialSurface(true, warningLevel), "meter");
});

test("trial surface is onboarding for a fresh unseen trial", () => {
  assert.equal(trialSurface(false, fresh), "onboarding");
});

test("trial surface is exhausted for a spent unseen trial", () => {
  assert.equal(trialSurface(false, spent), "exhausted");
});

test("trial surface is none when the trial does not apply to this user", () => {
  assert.equal(trialSurface(false, { ...fresh, applicable: false }), "none");
});
