import { strict as assert } from "node:assert";
import { test } from "node:test";
import { editorDocument, fieldsFor, lintManifest, pluralForKind, starter } from "./registry-schemas.ts";

test("user-authored artifacts are private by default and cannot select broader visibility", () => {
  const manifest = starter("Agent");
  const metadata = manifest.metadata as Record<string, unknown>;
  const visibility = fieldsFor("Agent").find((field) => field.path === "metadata.visibility");

  assert.equal(metadata.visibility, "private");
  assert.deepEqual(visibility?.options, ["private"]);
  assert.match(visibility?.help ?? "", /private/i);
});

test("editing starts from an independent copy of the published manifest", () => {
  const published = starter("Agent");
  const publishedSpec = published.spec as Record<string, unknown>;
  publishedSpec.systemPrompt = "Original prompt";

  const editing = editorDocument("Agent", published);
  const editingSpec = editing.spec as Record<string, unknown>;
  editingSpec.systemPrompt = "New version";

  assert.equal(publishedSpec.systemPrompt, "Original prompt");
  assert.equal(editingSpec.systemPrompt, "New version");
});

test("agent authoring accepts an inline system prompt or a prompt reference", () => {
  const manifest = starter("Agent");
  const metadata = manifest.metadata as Record<string, unknown>;
  const spec = manifest.spec as Record<string, unknown>;
  const promptRef = fieldsFor("Agent").find((field) => field.path === "spec.promptRef");
  metadata.name = "incident-agent";
  spec.title = "Incident agent";
  spec.model = { provider: "anthropic", name: "claude-sonnet-4-6", temperature: 0.3 };

  assert.equal(promptRef?.type, "ref");
  assert.equal(promptRef?.itemKind, "prompts");

  spec.systemPrompt = "Investigate the incident.";
  assert.equal(lintManifest(manifest, "Agent").filter((issue) => issue.level === "error").length, 0);

  spec.systemPrompt = "";
  spec.promptRef = "incident-prompt-v1";
  assert.equal(lintManifest(manifest, "Agent").filter((issue) => issue.level === "error").length, 0);

  spec.promptRef = "";
  assert.match(
    lintManifest(manifest, "Agent").find((issue) => issue.level === "error")?.message ?? "",
    /system prompt|prompt reference/i,
  );
});

test("agent authoring emits typed model limits and risk fields", () => {
  const manifest = starter("Agent");
  const metadata = manifest.metadata as Record<string, unknown>;
  const spec = manifest.spec as Record<string, unknown>;
  const fields = fieldsFor("Agent");

  assert.deepEqual(spec.model, { provider: "", name: "", temperature: null });
  assert.deepEqual(spec.limits, { maxTurns: 20, timeoutSeconds: 900 });
  assert.equal(spec.riskLevel, "medium");
  assert.equal(fields.find((field) => field.path === "spec.limits")?.type, "group");
  const modelFields = fields.find((field) => field.path === "spec.model")?.children ?? [];
  assert.ok(modelFields.find((field) => field.path === "provider")?.options?.includes("vertex_gemini"));
  assert.deepEqual(fields.find((field) => field.path === "spec.riskLevel")?.options, [
    "low",
    "medium",
    "high",
    "critical",
  ]);

  metadata.name = "review-agent";
  spec.title = "Review agent";
  spec.systemPrompt = "Review the change.";
  spec.model = { provider: "anthropic", name: "claude-sonnet-4-6", temperature: 0.3 };
  assert.equal(lintManifest(manifest, "Agent").filter((issue) => issue.level === "error").length, 0);
  spec.model = { provider: "claude", name: "claude-sonnet-4-6", temperature: 0.3 };
  assert.equal(lintManifest(manifest, "Agent").filter((issue) => issue.level === "error").length, 0);

  spec.model = { provider: "made-up", name: "claude-sonnet-4-6", temperature: "0.3" };
  spec.limits = { maxTurns: 0, timeoutSeconds: "900" };
  spec.riskLevel = "extreme";
  const messages = lintManifest(manifest, "Agent").map((issue) => issue.message).join("\n");
  assert.match(messages, /spec\.model\.provider/);
  assert.match(messages, /spec\.model\.temperature/);
  assert.match(messages, /spec\.limits\.maxTurns/);
  assert.match(messages, /spec\.limits\.timeoutSeconds/);
  assert.match(messages, /spec\.riskLevel/);
});

test("dataset and eval suite starters preserve immutable versioned references", () => {
  const dataset = starter("Dataset");
  const suite = starter("EvalSuite");

  assert.equal(pluralForKind("Dataset"), "datasets");
  assert.equal(pluralForKind("EvalSuite"), "eval-suites");
  assert.deepEqual(dataset.spec, { description: "", cases: [] });
  assert.deepEqual(suite.spec, {
    description: "",
    datasetRef: { ref: "", version: "" },
    scorers: [],
    thresholds: { success: null, safety: null, p95_latency_s: null, cost_per_run_usd: null },
  });
  assert.ok(fieldsFor("Dataset").some((field) => field.path === "spec.cases"));
  assert.ok(fieldsFor("EvalSuite").some((field) => field.path === "spec.datasetRef"));
});

test("eval artifact lint requires cases and an exact dataset version", () => {
  const dataset = starter("Dataset");
  const suite = starter("EvalSuite");

  assert.ok(lintManifest(dataset, "Dataset").some((issue) => issue.message.includes("spec.cases")));
  assert.ok(lintManifest(suite, "EvalSuite").some((issue) => issue.message.includes("datasetRef")));
});

test("eval suite lint rejects duplicate scorers and invalid structured thresholds", () => {
  const suite = starter("EvalSuite");
  const metadata = suite.metadata as Record<string, unknown>;
  const spec = suite.spec as Record<string, unknown>;
  metadata.name = "release-gate";
  spec.datasetRef = { ref: "golden", version: "3" };
  spec.scorers = ["exact_match", "exact_match"];
  spec.thresholds = { success: 1.1, safety: -0.1, p95_latency_s: 0, cost_per_run_usd: -1 };

  const messages = lintManifest(suite, "EvalSuite").map((issue) => issue.message).join("\n");

  assert.match(messages, /duplicate scorer/);
  assert.match(messages, /thresholds\.success/);
  assert.match(messages, /thresholds\.safety/);
  assert.match(messages, /thresholds\.p95_latency_s/);
  assert.match(messages, /thresholds\.cost_per_run_usd/);
});
