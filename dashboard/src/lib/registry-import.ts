type SearchAgentIdentity = {
  kind: string;
  name: string;
  namespace: string;
  version: string;
  arn: string;
};

type ImportedAgent = {
  agent?: { spec?: Record<string, unknown> };
};

export function registryAgentReference(hit: SearchAgentIdentity): string {
  if (hit.kind.toLowerCase() !== "agent") throw new Error("Only an Agent can be imported into a sandbox");
  if (!hit.version || hit.version.toLowerCase() === "latest") {
    throw new Error("Agent import needs an immutable version");
  }
  const match = /^arn:agentic:registry:([^:]+):agents\/([^/]+)\/([^/]+)$/.exec(hit.arn);
  const tenant = match?.[1] ?? "";
  if (!tenant || match?.[2] !== hit.namespace || match?.[3] !== hit.name) {
    throw new Error("Agent search result has no trustworthy Registry identity");
  }
  return `registry://${tenant}/agents/${hit.namespace}/${hit.name}@${hit.version}`;
}

export function importedAgentModel(imported: ImportedAgent): { provider: string; model: string } {
  const raw = imported.agent?.spec?.model;
  if (raw && typeof raw === "object") {
    const model = raw as Record<string, unknown>;
    const provider = typeof model.provider === "string" ? model.provider.trim() : "";
    const name = typeof model.name === "string" ? model.name.trim() : "";
    if (provider && name) return { provider, model: name };
  }
  return { provider: "portable", model: "external-runtime" };
}
