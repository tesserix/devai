const API_BASE = "/api";

export interface PipelineRun {
  run_id: string;
  stage: string;
  repo: string;
  created_at: string;
  agents: Record<string, { status: string; error?: string; updated_at?: number }>;
}

export interface A2AMessage {
  id: string;
  from_agent: string;
  to_agent: string;
  message_type: string;
  subject: string;
  body: string;
  timestamp: string;
}

// ── Blueprint graph (render-ready, served by GET /api/pipeline/blueprints/{name}) ──
// This is the source the pipeline-flow view renders from — a new (or UI-created)
// blueprint shows up with no UI code change.
export interface BlueprintGraphNode {
  name: string;
  stage: string;
  type: string;
  title: string;
  lane: string;
  color: string;
  agent: string;
  gate: boolean;
  depends_on: string[];
  condition: string | null;
  timeout_seconds: number | null;
  on_failure: string;
  parallel: boolean;
  parallel_group: string | null;
}

export interface BlueprintGraphEdge {
  from: string;
  to: string;
  kind: "sequence" | "conditional";
  label: string;
}

export interface BlueprintGraph {
  name: string;
  title: string;
  description: string;
  lanes: string[];
  nodes: BlueprintGraphNode[];
  edges: BlueprintGraphEdge[];
  levels: string[][];
}

export interface PipelineConfig {
  auto_mode: boolean;
  gates: Record<string, boolean>;
  claude_model: string;
  openai_model: string;
  groq_model: string;
  max_review_iterations: number;
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...opts,
    headers: { "Content-Type": "application/json", ...opts?.headers },
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

export const api = {
  // Auth
  me: () => apiFetch<{ login: string; name: string; avatar_url: string }>("/me"),

  // Pipeline
  listRuns: (repo?: string, limit = 20) =>
    apiFetch<PipelineRun[]>(`/pipeline/runs?limit=${limit}${repo ? `&repo=${repo}` : ""}`),

  getRun: (runId: string) => apiFetch<PipelineRun>(`/pipeline/runs/${runId}`),

  // Render-ready blueprint graph — the pipeline-flow view draws this directly.
  getBlueprintGraph: (name = "alm-pipeline") =>
    apiFetch<BlueprintGraph>(`/pipeline/blueprints/${encodeURIComponent(name)}`),

  // Per-run Repo viewer (powers the REPO tab on the run detail page).
  // Read-only — the dashboard cannot write to the repo from here.
  getRepoTree: (runId: string, path = "") =>
    apiFetch<{
      repo: string;
      branch: string;
      path: string;
      entries: Array<{ name: string; type: "file" | "dir"; size: number; path: string }>;
    }>(`/runs/${runId}/repo/tree?path=${encodeURIComponent(path)}`),

  getRepoFile: (runId: string, path: string) =>
    apiFetch<{
      repo: string;
      branch: string;
      path: string;
      encoding: "utf-8" | "base64";
      content: string;
      size: number;
      truncated: boolean;
    }>(`/runs/${runId}/repo/file?path=${encodeURIComponent(path)}`),

  triggerPipeline: (repo: string, requirements: string, issueNumber?: number) =>
    apiFetch<{ run_id: string; stage: string }>("/pipeline/trigger", {
      method: "POST",
      body: JSON.stringify({ repo, requirements, issue_number: issueNumber }),
    }),

  retriggerRun: (runId: string) =>
    apiFetch<{ run_id: string; stage: string; repo: string; retry_of: string }>(
      `/pipeline/runs/${runId}/retrigger`,
      { method: "POST" },
    ),

  pauseRun: (runId: string) =>
    apiFetch<{ run_id: string; control: string }>(`/pipeline/runs/${runId}/pause`, {
      method: "POST",
    }),

  resumeRun: (runId: string) =>
    apiFetch<{ run_id: string; control: string }>(`/pipeline/runs/${runId}/resume`, {
      method: "POST",
    }),

  stopRun: (runId: string) =>
    apiFetch<{ run_id: string; control: string }>(`/pipeline/runs/${runId}/stop`, {
      method: "POST",
    }),

  // Config
  getConfig: (repo = "default") => apiFetch<PipelineConfig>(`/pipeline/config?repo=${repo}`),

  saveConfig: (config: Partial<PipelineConfig> & { repo?: string }) =>
    apiFetch<{ status: string }>("/pipeline/config", {
      method: "POST",
      body: JSON.stringify(config),
    }),

  // Approvals
  getApprovals: (runId: string) =>
    apiFetch<Array<{ gate: string; agent: string; timestamp: number }>>(`/pipeline/runs/${runId}/approvals`),

  approveGate: (runId: string, gate: string) =>
    apiFetch<{ status: string }>(`/pipeline/runs/${runId}/approvals/${gate}/approve`, { method: "POST" }),

  rejectGate: (runId: string, gate: string) =>
    apiFetch<{ status: string }>(`/pipeline/runs/${runId}/approvals/${gate}/reject`, { method: "POST" }),

  // Orgs & Repos
  getOrgs: () => apiFetch<Array<{ login: string; avatar_url: string }>>("/orgs"),

  getRepos: (org: string) =>
    apiFetch<Array<{ full_name: string; name: string; description: string; language: string }>>(
      `/orgs/${org}/repos`
    ),

  // GitHub App repos (installation-level, no OAuth needed)
  listRepos: () =>
    apiFetch<Array<{ full_name: string; name: string; description: string; language: string; private: boolean }>>(
      "/repos"
    ),

  checkRepoName: (name: string, org = "tesserix") =>
    apiFetch<{ available: boolean; reason: string }>(`/repos/check?name=${encodeURIComponent(name)}&org=${encodeURIComponent(org)}`),

  createRepo: (org: string, name: string, description?: string, isPrivate?: boolean) =>
    apiFetch<{ full_name: string; name: string; html_url: string }>("/repos/create", {
      method: "POST",
      body: JSON.stringify({ org, name, description, private: isPrivate ?? true }),
    }),

  // GitHub Projects v2
  listProjects: () =>
    apiFetch<Array<{ id: string; title: string; number: number; description: string; url: string }>>(
      "/projects"
    ),

  createProject: (title: string, description?: string, repo?: string) =>
    apiFetch<{ id: string; number: number; url: string; title: string }>("/projects/create", {
      method: "POST",
      body: JSON.stringify({ title, description, repo }),
    }),

  // Repo scaffolding
  scaffoldRepo: (repo: string, projectTitle?: string, techStack?: string) =>
    apiFetch<{ status: string; repo: string; files_created: string[]; project: unknown }>(
      "/repos/scaffold",
      {
        method: "POST",
        body: JSON.stringify({ repo, project_title: projectTitle, tech_stack: techStack }),
      }
    ),

  // ── Repos onboarding ──────────────────────────────────────────────
  // Org catalog with onboarding status overlay (GH App access list +
  // .platform/devai.yaml marker probe), the onboard action (gated PR),
  // and merge / assign-reviewer from inside DevAI.
  listOrgRepos: (opts: { q?: string; page?: number; per_page?: number; refresh?: boolean; probe?: boolean } = {}) => {
    const p = new URLSearchParams();
    if (opts.q) p.set("q", opts.q);
    p.set("page", String(opts.page ?? 1));
    p.set("per_page", String(opts.per_page ?? 30));
    if (opts.refresh) p.set("refresh", "1");
    p.set("probe", opts.probe === false ? "0" : "1");
    return apiFetch<RepoCatalogPage>(`/scm/org/repos?${p.toString()}`);
  },

  listOnboarded: (state?: string) =>
    apiFetch<OnboardedRepo[]>(`/scm/onboarded${state ? `?state=${encodeURIComponent(state)}` : ""}`),

  getOnboarded: (owner: string, name: string) =>
    apiFetch<OnboardedRepo>(`/scm/onboarded/${owner}/${name}`),

  onboardRepos: (
    repos: Array<{ owner: string; name: string }>,
    opts: { base_branch?: string; description?: string; dry_run?: boolean } = {}
  ) =>
    apiFetch<OnboardResult>("/scm/onboarded", {
      method: "POST",
      body: JSON.stringify({ repos, ...opts }),
    }),

  markOnboardingPRReady: (owner: string, name: string) =>
    apiFetch<OnboardedRepo>(`/scm/onboarded/${owner}/${name}/ready`, {
      method: "POST",
    }),

  mergeOnboardingPR: (owner: string, name: string, method = "squash") =>
    apiFetch<OnboardedRepo>(`/scm/onboarded/${owner}/${name}/merge`, {
      method: "POST",
      body: JSON.stringify({ method }),
    }),

  assignReviewer: (owner: string, name: string, reviewers: string[]) =>
    apiFetch<{ repo: string; pr_number: number; reviewers: string[] }>(
      `/scm/onboarded/${owner}/${name}/assign`,
      { method: "POST", body: JSON.stringify({ reviewers }) }
    ),

  archiveOnboarded: (owner: string, name: string) =>
    apiFetch<OnboardedRepo>(`/scm/onboarded/${owner}/${name}`, { method: "DELETE" }),

  reconcileRepos: (org?: string) =>
    apiFetch<{ scanned: number; reconciled: number; skipped: number; errors: string[] }>(
      `/scm/onboarded/reconcile${org ? `?org=${encodeURIComponent(org)}` : ""}`,
      { method: "POST" }
    ),

  // ── Catalog (tools available to pick when authoring an agent) ──────
  listCatalogTools: (category?: string) =>
    apiFetch<CatalogTool[]>(`/catalog/tools${category ? `?category=${encodeURIComponent(category)}` : ""}`),

  // ── Registry catalog (skills/prompts/MCP servers to compose into an agent) ──
  listRegistrySkills: () => apiFetch<RegistryItem[]>("/registry/skills"),
  listRegistryPrompts: () => apiFetch<RegistryItem[]>("/registry/prompts"),
  listRegistryMcpServers: () => apiFetch<RegistryItem[]>("/registry/mcp-servers"),
  listRegistryAgents: () => apiFetch<RegistryItem[]>("/registry/agents"),

  // ── Authoring: custom skills ──────────────────────────────────────
  createSkill: (input: CreateSkillInput) =>
    apiFetch<{ name: string; title?: string }>("/authoring/skills", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  listAuthoredSkills: () => apiFetch<AuthoredDefinition[]>("/authoring/skills"),
  deleteSkill: (name: string) =>
    apiFetch<{ deleted: string }>(`/authoring/skills/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // ── Authoring: custom tools (MCP servers) ─────────────────────────
  createMcpServer: (input: CreateMcpServerInput) =>
    apiFetch<{ name: string; type?: string }>("/authoring/mcp-servers", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  listAuthoredMcpServers: () => apiFetch<AuthoredDefinition[]>("/authoring/mcp-servers"),
  deleteMcpServer: (name: string) =>
    apiFetch<{ deleted: string }>(`/authoring/mcp-servers/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // ── Authoring: custom agents (specializations) ────────────────────
  listAuthoredAgents: () => apiFetch<AuthoredDefinition[]>("/authoring/specializations"),

  createAgent: (input: CreateAgentInput) =>
    apiFetch<{ name: string; display_name?: string; category?: string }>(
      "/authoring/specializations",
      { method: "POST", body: JSON.stringify(input) }
    ),

  deleteAgent: (name: string) =>
    apiFetch<{ deleted: string }>(`/authoring/specializations/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  // ── Authoring: custom blueprints (workflows) ──────────────────────
  listAuthoredBlueprints: () => apiFetch<AuthoredDefinition[]>("/authoring/blueprints"),

  createBlueprint: (yaml: string) =>
    apiFetch<{ name: string; stages: number }>("/authoring/blueprints", {
      method: "POST",
      body: JSON.stringify({ yaml }),
    }),

  deleteBlueprint: (name: string) =>
    apiFetch<{ deleted: string }>(`/authoring/blueprints/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
};

// ── Authoring types ───────────────────────────────────────────────
export interface CatalogTool {
  name: string;
  description: string;
  category: string;
  required: string[];
  parameters: string[];
  schema: Record<string, unknown>;
}

export interface HandoverFieldInput {
  type: string;
  required: boolean;
  description?: string;
}

export interface CreateAgentInput {
  name: string;
  display_name?: string;
  description?: string;
  category?: string;
  llm_provider?: string;
  llm_model?: string;
  temperature?: number | null;
  system_prompt?: string;
  allowed_tools?: string[];
  handover_schema?: Record<string, HandoverFieldInput>;
  risk_level?: string;
  output_key?: string;
  // Registry-composition picks from the shared catalog.
  skills?: string[];
  prompts?: string[];
  mcp_servers?: string[];
}

// A registry catalog item (skill/prompt/mcp-server/agent) as flattened by
// the /api/registry/* routes — name + a human label + description.
export interface RegistryItem {
  name: string;
  title?: string;
  display_name?: string;
  description?: string;
  version?: string;
  [key: string]: unknown;
}

export interface CreateSkillInput {
  name: string;
  title?: string;
  description?: string;
  category?: string;
  tags?: string[];
  repository?: string;
}

export interface CreateMcpServerInput {
  name: string;
  version?: string;
  description?: string;
  url?: string;
  transport?: string;
  headers?: Record<string, string>;
  image?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
}

export interface AuthoredDefinition {
  kind: string;
  name: string;
  yaml: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export type OnboardingStateValue =
  | "discovered"
  | "pending_pr"
  | "onboarded"
  | "archived"
  | "dormant";

export interface CatalogRepo {
  owner: string;
  name: string;
  full_name: string;
  default_branch: string;
  description: string;
  language: string;
  private: boolean;
  archived: boolean;
  html_url: string;
  pushed_at: string;
  has_marker: boolean | null;
  state: OnboardingStateValue;
  onboarded: boolean;
  pr_number: number | null;
  pr_url: string;
  draft?: boolean;
}

export interface RepoCatalogPage {
  repos: CatalogRepo[];
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  has_prev: boolean;
  has_next: boolean;
  onboarded_count: number;
  cache_hit: boolean;
  org: string;
}

export interface OnboardedRepo {
  owner: string;
  name: string;
  full_name: string;
  state: OnboardingStateValue;
  onboarded: boolean;
  pr_number: number | null;
  pr_url: string;
  draft?: boolean;
  default_base_branch: string;
  description: string;
  onboarded_at: string | null;
  onboarded_by: string;
  tags: string[];
}

export interface OnboardResult {
  succeeded: Array<{ repo: string; status: string; state?: string; pr_number?: number; pr_url?: string }>;
  failed: Array<{ repo: string; error: string }>;
  dry_run: boolean;
}
