import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  registryAgentManifestPath,
  registryAgentsPath,
  registryArtifactPath,
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
