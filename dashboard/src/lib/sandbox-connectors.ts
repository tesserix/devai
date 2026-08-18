import type { SettingsConnector } from "./api";

const PROVIDER_SECRET: Record<string, string> = {
  anthropic: "anthropic_api_key",
  openai: "openai_api_key",
  vertex_gemini: "vertex_api_key",
  groq: "groq_api_key",
  openrouter: "openrouter_api_key",
  gateway: "llm_gateway_api_key",
};

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  vertex_gemini: "Vertex AI",
  groq: "Groq",
  openrouter: "OpenRouter",
  gateway: "AgentGateway",
};

const PROVIDER_ALIAS: Record<string, string> = {
  claude: "anthropic",
  codex: "openai",
  gemini: "vertex_gemini",
  vertex: "vertex_gemini",
  google: "vertex_gemini",
};

export type SandboxConnectorOption = {
  value: string;
  label: string;
  description: string;
  provider: string;
};

export function canonicalSandboxProvider(provider: string): string {
  const normalized = provider.trim().toLowerCase();
  return PROVIDER_ALIAS[normalized] ?? normalized;
}

export function sandboxLlmConnectorOptions(connectors: SettingsConnector[]): SandboxConnectorOption[] {
  return connectors
    .filter((connector) => {
      const requiredSecret = PROVIDER_SECRET[canonicalSandboxProvider(connector.provider)];
      return (
        connector.scope === "user" &&
        connector.connector_key === "llm" &&
        connector.enabled &&
        Boolean(requiredSecret) &&
        connector.secrets_set.includes(requiredSecret)
      );
    })
    .map((connector) => {
      const provider = canonicalSandboxProvider(connector.provider);
      return {
        value: connector.instance_id,
        label: `${PROVIDER_LABEL[provider] ?? provider} · ${connector.instance_id}`,
        description: "Your user-scoped connector; its key stays in the DevAI control plane.",
        provider,
      };
    });
}
