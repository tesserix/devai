/**
 * Links from DevAI catalog cards out to the Agentic Registry's own marketplace
 * UI, so clicking a skill / agent / prompt / tool / MCP server opens its detail
 * page in the registry. The base URL is configurable via the build env and
 * defaults to the prod marketplace. The `?namespace=` scopes the detail lookup
 * to the tenant the catalog is published under (devai), so it resolves even
 * when the marketplace's default (unscoped) list is empty.
 */
export const AREGISTRY_BASE =
  process.env.NEXT_PUBLIC_AREGISTRY_URL?.replace(/\/+$/, "") || "https://aregistry.tesserix.app";

export const AREGISTRY_NAMESPACE = process.env.NEXT_PUBLIC_AREGISTRY_NAMESPACE || "devai";

export type AregistryKind =
  | "skills"
  | "agents"
  | "prompts"
  | "mcp-servers"
  | "tools"
  | "blueprints"
  | "workflows";

/** Detail-page URL in the registry marketplace for a catalog object. */
export function aregistryUrl(kind: AregistryKind, name: string): string {
  const ns = encodeURIComponent(AREGISTRY_NAMESPACE);
  return `${AREGISTRY_BASE}/${kind}/${encodeURIComponent(name)}?namespace=${ns}`;
}
