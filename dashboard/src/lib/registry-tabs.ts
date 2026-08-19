export const REGISTRY_TABS = ["skills", "prompts", "mcp-servers", "agents", "datasets", "eval-suites"] as const;

export type RegistryTab = (typeof REGISTRY_TABS)[number];

export function isRegistryTab(value: string): value is RegistryTab {
  return REGISTRY_TABS.some((tab) => tab === value);
}

export function registryEndpoint(tab: RegistryTab): string {
  return `/api/registry/${tab}`;
}
