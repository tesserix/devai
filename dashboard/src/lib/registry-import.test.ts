import assert from "node:assert/strict";
import test from "node:test";

import { importedAgentModel, registryAgentReference } from "./registry-import.ts";

test("builds the immutable import reference from the authorized search hit ARN", () => {
  assert.equal(
    registryAgentReference({
      kind: "Agent",
      name: "support",
      namespace: "customer-ai",
      version: "1.4.0",
      arn: "arn:agentic:registry:acme:agents/customer-ai/support",
    }),
    "registry://acme/agents/customer-ai/support@1.4.0",
  );
});

test("refuses mutable or malformed search hits", () => {
  assert.throws(
    () => registryAgentReference({ kind: "Agent", name: "support", namespace: "acme", version: "latest", arn: "" }),
    /immutable/,
  );
  assert.throws(
    () => registryAgentReference({ kind: "Tool", name: "search", namespace: "acme", version: "1", arn: "" }),
    /Agent/,
  );
});

test("pins the published model when present and uses a visible portable fallback", () => {
  assert.deepEqual(
    importedAgentModel({ agent: { spec: { model: { provider: "openai", name: "gpt-5-mini" } } } }),
    { provider: "openai", model: "gpt-5-mini" },
  );
  assert.deepEqual(importedAgentModel({ agent: { spec: {} } }), {
    provider: "portable",
    model: "external-runtime",
  });
});
