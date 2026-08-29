import { strict as assert } from "node:assert";
import { test } from "node:test";
import { REGISTRY_TABS, registryEndpoint } from "./registry-tabs.ts";

test("registry discovery includes versioned datasets and eval suites", () => {
  assert.ok(REGISTRY_TABS.includes("datasets"));
  assert.ok(REGISTRY_TABS.includes("eval-suites"));
  assert.equal(registryEndpoint("datasets"), "/api/registry/datasets");
  assert.equal(registryEndpoint("eval-suites"), "/api/registry/eval-suites");
});
