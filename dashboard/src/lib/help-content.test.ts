import assert from "node:assert/strict";
import test from "node:test";

import { evaluationMetricHelpTerm, getHelpTerm } from "./help-content.ts";

test("evaluation metrics resolve to an in-context explanation", () => {
  const expected = {
    pass_rate: "evaluation-pass-rate",
    success: "evaluation-pass-rate",
    p95_latency_ms: "evaluation-p95-latency",
    total_tokens: "evaluation-tokens",
    tokens: "evaluation-tokens",
    cost_usd: "evaluation-cost",
    exact_match: "evaluation-deterministic-score",
    tool_trajectory: "evaluation-trajectory-score",
    safety: "evaluation-safety-score",
    groundedness: "evaluation-groundedness",
    helpfulness: "evaluation-scorer-dimension",
  } as const;

  for (const [metric, term] of Object.entries(expected)) {
    assert.equal(evaluationMetricHelpTerm(metric), term);
    assert.ok(getHelpTerm(term), `${term} must have help content`);
  }
});

test("comparison-only concepts have in-context explanations", () => {
  for (const term of [
    "evaluation-delta",
    "evaluation-regression",
    "evaluation-sample-size",
  ]) {
    const entry = getHelpTerm(term);
    assert.ok(entry, `${term} must have help content`);
    assert.ok(entry.summary.length > 20);
  }
});
