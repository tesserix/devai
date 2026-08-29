import assert from "node:assert/strict";
import test from "node:test";

import { feedbackInboxTitle, feedbackStatusLabel } from "./feedback.ts";

test("feedback status labels are user-facing", () => {
  assert.equal(feedbackStatusLabel("open"), "Open");
  assert.equal(feedbackStatusLabel("closed"), "Resolved");
});

test("support staff see an inbox while users see their own requests", () => {
  assert.equal(feedbackInboxTitle(false), "Your feedback");
  assert.equal(feedbackInboxTitle(true), "Support inbox");
});
