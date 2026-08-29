import assert from "node:assert/strict";
import test from "node:test";
import { canonicalSandboxProvider, sandboxLlmConnectorOptions } from "./sandbox-connectors.ts";
import type { SettingsConnector } from "./api";

function connector(overrides: Partial<SettingsConnector>): SettingsConnector {
  return {
    scope: "user",
    scope_id: "alice",
    connector_key: "llm",
    provider: "anthropic",
    instance_id: "default",
    prefs: {},
    secrets_set: ["anthropic_api_key"],
    enabled: true,
    updated_by: "alice@example.com",
    updated_at: "",
    ...overrides,
  };
}

test("sandbox connector options include only enabled user-owned keyed LLM connectors", () => {
  const options = sandboxLlmConnectorOptions([
    connector({ instance_id: "sandbox-evals" }),
    connector({ scope: "tenant", instance_id: "shared" }),
    connector({ provider: "vertex_gemini", instance_id: "keyless", secrets_set: [] }),
    connector({ instance_id: "disabled", enabled: false }),
    connector({ connector_key: "scm", instance_id: "scm" }),
  ]);

  assert.deepEqual(options, [
    {
      value: "sandbox-evals",
      label: "Anthropic · sandbox-evals",
      description: "Your user-scoped connector; its key stays in the DevAI control plane.",
      provider: "anthropic",
    },
  ]);
});

test("each provider requires its own credential before it can be selected", () => {
  const options = sandboxLlmConnectorOptions([
    connector({ provider: "openai", instance_id: "openai", secrets_set: ["openai_api_key"] }),
    connector({ provider: "gateway", instance_id: "gateway", secrets_set: ["llm_gateway_api_key"] }),
    connector({ provider: "groq", instance_id: "groq", secrets_set: ["anthropic_api_key"] }),
  ]);

  assert.deepEqual(options.map((option) => option.value), ["openai", "gateway"]);
});

test("registry provider aliases select the matching connector family", () => {
  assert.deepEqual(
    ["claude", "codex", "gemini", "vertex", "google"].map(canonicalSandboxProvider),
    ["anthropic", "openai", "vertex_gemini", "vertex_gemini", "vertex_gemini"],
  );
});
