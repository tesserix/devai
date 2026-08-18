import { strict as assert } from "node:assert";
import { test } from "node:test";
import { registryAgentsPath } from "./api.ts";

test("requests the authenticated user's agents only when mine is selected", () => {
  assert.equal(registryAgentsPath(true), "/registry/agents?mine=true");
  assert.equal(registryAgentsPath(false), "/registry/agents");
});
