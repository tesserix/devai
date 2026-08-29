import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  registryAgentRuntimeStatusPath,
  comparisonPath,
  evaluationRunPath,
  registryAgentManifestPath,
  registryAgentsPath,
  registryArtifactPath,
  registrySearchPath,
  agentImportsPath,
  sandboxPath,
  sandboxTracesPath,
  lifecycleMutationHeaders,
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

test("scopes runtime status to the selected agent view", () => {
  assert.equal(registryAgentRuntimeStatusPath(true), "/registry/agents/runtime-status?mine=true");
  assert.equal(registryAgentRuntimeStatusPath(false), "/registry/agents/runtime-status");
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

test("encodes semantic search and project-scoped immutable imports", () => {
  assert.equal(
    registrySearchPath("incident response", ["Agent", "Tool"], 20),
    "/registry/search?q=incident+response&kinds=Agent%2CTool&limit=20",
  );
  assert.equal(
    agentImportsPath("support / lab"),
    "/registry/imports?project_id=support+%2F+lab",
  );
});

test("carries a caller-stable idempotency key on durable mutations", () => {
  assert.deepEqual(lifecycleMutationHeaders("request-42"), {
    "Idempotency-Key": "request-42",
  });
});
