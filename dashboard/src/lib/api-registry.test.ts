import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  comparisonPath,
  evaluationRunPath,
  registryAgentManifestPath,
  registryAgentsPath,
  registryArtifactPath,
  sandboxPath,
  sandboxTracesPath,
} from "./api.ts";

test("requests the authenticated user's agents only when mine is selected", () => {
  assert.equal(registryAgentsPath(true), "/registry/agents?mine=true");
  assert.equal(registryAgentsPath(false), "/registry/agents");
});

test("encodes agent lifecycle paths without changing their collection", () => {
  assert.equal(
    registryAgentManifestPath("my agent"),
    "/registry/agents/my%20agent/manifest",
  );
  assert.equal(
    registryArtifactPath("agents", "my/agent"),
    "/registry/agents/my%2Fagent",
  );
});

test("encodes owner-scoped sandbox, trace, evaluation, and comparison paths", () => {
  assert.equal(sandboxPath("sandbox/one"), "/sandboxes/sandbox%2Fone");
  assert.equal(
    sandboxTracesPath("sandbox/one", 25),
    "/sandboxes/sandbox%2Fone/traces?limit=25",
  );
  assert.equal(evaluationRunPath("eval/one"), "/evaluations/eval%2Fone");
  assert.equal(comparisonPath("compare/one"), "/comparisons/compare%2Fone");
});
