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

// Lightweight blueprint summary for the picker (DASH-6) and the Workflows home.
// name + human title + one-line description + stage count, derived by
// listBlueprints() from the catalog (and/or the graph node count).
export interface BlueprintSummary {
  name: string;
  title: string;
  description: string;
  stageCount: number;
  /** Lanes the blueprint spans (for a compact affordance). */
  lanes?: string[];
}

// Filters accepted by listRuns(); the backend already supports all of these.
export interface RunFilters {
  blueprint?: string;
  repo?: string;
  state?: string;
  source?: string;
  limit?: number;
  offset?: number;
}

// The supervisor stage's advisory delegation plan (DASH-12). Surfaced on the
// run detail so the LLM's reasoning is visible — it does NOT change the DAG.
export interface DelegationAssignment {
  stage?: string;
  agent?: string;
  task?: string;
  rationale?: string;
  [key: string]: unknown;
}
export interface DelegationPlan {
  summary?: string;
  assignments: DelegationAssignment[];
  tracking_issue_url?: string | null;
  /** Free-form extras the backend may attach. */
  [key: string]: unknown;
}

export interface PipelineConfig {
  auto_mode: boolean;
  gates: Record<string, boolean>;
  claude_model: string;
  openai_model: string;
  groq_model: string;
  max_review_iterations: number;
}

// Centralized 401 handling (DASH-11). When the session has expired the backend
// returns 401 on any API call; rather than surfacing a raw "API 401" in every
// widget, we bounce the browser to /login with a return_to so the user lands
// back where they were. Guarded so it fires once and never during SSR.
let redirectingToLogin = false;
function handleUnauthorized(): void {
  if (typeof window === "undefined") return;
  if (redirectingToLogin) return;
  // Never redirect-loop when we're already on the login page.
  if (window.location.pathname.startsWith("/login")) return;
  redirectingToLogin = true;
  const returnTo = window.location.pathname + window.location.search;
  window.location.assign(`/login?return_to=${encodeURIComponent(returnTo)}`);
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...opts,
    headers: { "Content-Type": "application/json", ...opts?.headers },
  });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("Session expired — redirecting to sign in.");
  }
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

// Turn a failed response into a short, human-readable message. Crucially, this
// never surfaces a raw HTML error page (e.g. a Cloudflare 502 served when the
// backend is slow or restarting) — those dump hundreds of lines of markup into
// the UI. We extract the JSON `detail` when present, drop HTML bodies, and give
// gateway statuses a friendly retry hint.
async function errorMessage(res: Response): Promise<string> {
  let detail = "";
  try {
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const body = await res.json();
      detail =
        typeof body?.detail === "string"
          ? body.detail
          : body?.detail
            ? JSON.stringify(body.detail)
            : typeof body?.message === "string"
              ? body.message
              : "";
    } else {
      const text = (await res.text()).trim();
      // Skip HTML (gateway/error pages) — only keep short plain-text bodies.
      if (text && !text.startsWith("<")) detail = text.slice(0, 200);
    }
  } catch {
    /* body unreadable — fall through to the status-only message */
  }
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    return `Service temporarily unavailable (${res.status}). The backend may be busy or starting up — try again in a moment.${
      detail ? ` · ${detail}` : ""
    }`;
  }
  return `API ${res.status}${detail ? `: ${detail}` : ""}`;
}

// Map a run from any backend shape (legacy {run_id,stage,agents} or Fiber
// {id,state,current_stage}) into the PipelineRun the dashboard renders. Always
// returns a string run_id/stage so downstream .slice()/.replace() are safe.
function normalizeRun(raw: unknown): PipelineRun {
  const r = (raw ?? {}) as Record<string, unknown>;
  const runId = (r.run_id ?? r.id ?? r.task_id ?? "") as string;
  // `||` not `??`: a terminal run clears `current_stage` to "" (empty, not
  // null), and `??` would stop there — leaving stage="" which the badge maps to
  // "pending". That made finished runs read PENDING in the Fleet list (and hid
  // the Retry button, which is gated on isFailed). Fall through empty strings so
  // a failed/completed run surfaces its `state` (stage_failed / completed).
  const stage = ((r.stage as string) || (r.current_stage as string) || (r.state as string) || "unknown");
  const agents =
    (r.agents as PipelineRun["agents"]) ??
    (r.agent_statuses as PipelineRun["agents"]) ??
    {};
  return {
    ...(r as object),
    run_id: runId,
    stage,
    repo: (r.repo as string) ?? "",
    created_at: (r.created_at as string) ?? "",
    agents,
  } as PipelineRun;
}

export const api = {
  // Auth
  me: () => apiFetch<{ login: string; name: string; avatar_url: string }>("/me"),

  // ── Settings (per-user/per-tenant connectors + secrets) ─────────────
  getSettingsCatalog: () =>
    apiFetch<SettingsCatalog>("/settings/catalog"),

  listSettings: () =>
    apiFetch<{ connectors: SettingsConnector[]; secrets_writable: boolean }>("/settings"),

  saveConnector: (body: SaveConnectorInput) =>
    apiFetch<{ status: string; connector: SettingsConnector }>("/settings/connectors", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteConnector: (scope: string, scopeId: string, connectorKey: string, instanceId = "default") =>
    apiFetch<{ status: string }>(
      `/settings/connectors/${scope}/${encodeURIComponent(scopeId || "-")}/${connectorKey}?instance_id=${encodeURIComponent(instanceId)}`,
      { method: "DELETE" }
    ),

  // Pipeline. The Fiber pipeline serializes a task as {id, state, current_stage,
  // …} but the dashboard's PipelineRun shape is {run_id, stage, agents}. Normalize
  // at the boundary so a missing run_id never reaches run.run_id.slice() and
  // white-screens the page.
  //
  // Overloaded for back-compat: the legacy callers pass (repo?, limit?); the new
  // /runs index (DASH-8) passes a RunFilters object. Both go to the same
  // backend query which already supports blueprint/repo/state/source/limit/offset.
  listRuns: async (filtersOrRepo?: RunFilters | string, limit = 20) => {
    const f: RunFilters =
      typeof filtersOrRepo === "string" || filtersOrRepo === undefined
        ? { repo: filtersOrRepo as string | undefined, limit }
        : filtersOrRepo;
    const p = new URLSearchParams();
    p.set("limit", String(f.limit ?? 20));
    if (f.offset) p.set("offset", String(f.offset));
    if (f.repo) p.set("repo", f.repo);
    if (f.blueprint) p.set("blueprint", f.blueprint);
    if (f.state) p.set("state", f.state);
    if (f.source) p.set("source", f.source);
    const raw = await apiFetch<unknown[]>(`/pipeline/runs?${p.toString()}`);
    return (Array.isArray(raw) ? raw : []).map(normalizeRun);
  },

  getRun: async (runId: string) =>
    normalizeRun(await apiFetch<unknown>(`/pipeline/runs/${runId}`)),

  // The supervisor stage's advisory delegation plan (DASH-12). Returns null when
  // the run carries none (e.g. a non-supervisor blueprint). Read from the run's
  // agent_context — the backend exposes it under the run detail.
  getDelegationPlan: async (runId: string): Promise<DelegationPlan | null> => {
    try {
      return await apiFetch<DelegationPlan>(`/pipeline/runs/${encodeURIComponent(runId)}/delegation-plan`);
    } catch {
      return null;
    }
  },

  // A2A message timeline for a run (DASH-7) — replaces the hardcoded
  // /dashboard/api fetch in the run detail page.
  getRunA2A: (runId: string) =>
    apiFetch<A2AMessage[]>(`/pipeline/runs/${encodeURIComponent(runId)}/a2a`),

  // List blueprints for the picker (DASH-6) + the Workflows home. Returns a
  // compact summary (name + title + description + stage count). The backend
  // serves the catalog at /pipeline/blueprints; we normalize the field names
  // and derive stageCount from whichever shape comes back.
  listBlueprints: async (): Promise<BlueprintSummary[]> => {
    const raw = await apiFetch<unknown[]>("/pipeline/blueprints");
    return (Array.isArray(raw) ? raw : []).map((item) => {
      const b = (item ?? {}) as Record<string, unknown>;
      const stageCount =
        (b.stage_count as number) ??
        (b.stageCount as number) ??
        (Array.isArray(b.stages) ? (b.stages as unknown[]).length : 0) ??
        (Array.isArray(b.nodes) ? (b.nodes as unknown[]).length : 0) ??
        0;
      return {
        name: (b.name as string) ?? "",
        title: (b.title as string) ?? (b.display_name as string) ?? (b.name as string) ?? "",
        description: (b.description as string) ?? "",
        stageCount,
        lanes: Array.isArray(b.lanes) ? (b.lanes as string[]) : undefined,
      };
    });
  },

  // Render-ready blueprint graph — the BlueprintDAG / pipeline-flow views draw
  // this directly.
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

  // Dispatch a run. Now threads the chosen blueprint (DASH-6) so the user
  // actually picks app-scaffold / crew-task / supervisor-alm instead of the
  // backend silently defaulting. `blueprint` is optional for back-compat.
  triggerPipeline: (
    repo: string,
    requirements: string,
    opts: { issueNumber?: number; blueprint?: string } = {},
  ) =>
    apiFetch<{ run_id: string; stage: string; blueprint?: string }>("/pipeline/trigger", {
      method: "POST",
      body: JSON.stringify({
        repo,
        requirements,
        issue_number: opts.issueNumber,
        blueprint: opts.blueprint,
      }),
    }),

  // Inject an additional instruction into a running pipeline (DASH-10). The
  // backend merges it into agent_context['injections'] for the next re-planning
  // stage to read. Returns 501 if the active blueprint has no consumer — the
  // caller surfaces that honestly rather than pretending it landed.
  injectMessage: (runId: string, message: string) =>
    apiFetch<{ run_id: string; injected: boolean }>(
      `/pipeline/runs/${encodeURIComponent(runId)}/inject`,
      { method: "POST", body: JSON.stringify({ message }) },
    ),

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

  deleteRun: (runId: string) =>
    apiFetch<{ run_id: string; deleted: boolean }>(`/pipeline/runs/${runId}`, {
      method: "DELETE",
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

  // Create a brand-new repo from scratch: scaffolds README, .github workflows
  // (CI/PR/release quality gates), Dependabot/CODEOWNERS, drops the
  // .platform/devai.yaml marker, and records it as ONBOARDED immediately.
  createAndOnboardRepo: (input: {
    name: string;
    description?: string;
    private?: boolean;
    tech_stack?: string;
  }) =>
    apiFetch<CreateRepoResult>("/scm/onboarded/create", {
      method: "POST",
      body: JSON.stringify({ private: true, ...input }),
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

  // Generic registry list (used by the artifact editor's reference pickers).
  listRegistry: (plural: string) => apiFetch<RegistryItem[]>(`/registry/${plural}`),

  // Publish a registry CR manifest (skills/agents/prompts/mcp-servers/…). The
  // backend stamps the tenant namespace and forwards to the registry, so the
  // artifact reflects in both DevAI and the aregistry marketplace. Names are
  // unique within the tenant: a create that collides returns 409 — pass
  // overwrite to publish a new version of the existing artifact on purpose.
  publishArtifact: (plural: string, manifest: unknown, overwrite = false) =>
    apiFetch<{ name?: string; status?: string }>(
      `/registry/${plural}${overwrite ? "?overwrite=true" : ""}`,
      { method: "POST", body: JSON.stringify(manifest) }
    ),

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

  // ── Teams + crews (the human team → AI crew model) ──────────────────
  listTeams: () => apiFetch<Team[]>("/teams"),

  listCrews: (teamId: string) => apiFetch<Crew[]>(`/teams/${encodeURIComponent(teamId)}/crews`),

  // ── Composer dispatch (Cursor-style /compose) ───────────────────────
  // Posts intent + @-context + image keys + team→crew selection to the
  // same pipeline dispatch endpoint the webhook/Kanban paths use.
  dispatchCompose: (body: ComposeRequest) =>
    apiFetch<DispatchResult>("/pipeline/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Roll the working tree back to a prior checkpoint (backend endpoint
  // lands in Phase 5; the timeline's ↩ button calls this).
  rollbackTo: (taskId: string, sha: string) =>
    apiFetch<{ task_id: string; sha: string; status: string }>(
      `/pipeline/runs/${encodeURIComponent(taskId)}/rollback`,
      { method: "POST", body: JSON.stringify({ sha }) }
    ),

  // ── SRE Studio: author → dry-run → publish ──────────────────────
  // Drafts live in DevAI; publish pushes to the shared registry which the
  // SRE runtime consumes + schedules.
  sreStudio: {
    listDrafts: (status?: string) =>
      apiFetch<SREDraft[]>(`/sre-studio/drafts${status ? `?status=${encodeURIComponent(status)}` : ""}`),
    getDraft: (id: string) => apiFetch<SREDraft>(`/sre-studio/drafts/${encodeURIComponent(id)}`),
    createDraft: (input: { kind: "blueprint" | "agent"; yaml: string; description?: string }) =>
      apiFetch<SREDraft>("/sre-studio/drafts", { method: "POST", body: JSON.stringify(input) }),
    updateDraft: (id: string, input: { yaml?: string; name?: string; description?: string }) =>
      apiFetch<SREDraft>(`/sre-studio/drafts/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify(input),
      }),
    deleteDraft: (id: string) =>
      apiFetch<{ deleted: string }>(`/sre-studio/drafts/${encodeURIComponent(id)}`, { method: "DELETE" }),
    dryRun: (id: string, clusterId = "default") =>
      apiFetch<SREDryRunPreview>(`/sre-studio/drafts/${encodeURIComponent(id)}/dry-run`, {
        method: "POST",
        body: JSON.stringify({ cluster_id: clusterId }),
      }),
    publish: (id: string) =>
      apiFetch<{ published: boolean; kind: string; name: string }>(
        `/sre-studio/drafts/${encodeURIComponent(id)}/publish`,
        { method: "POST" }
      ),
  },

  // ── Analytics: read-only rollups for the /analytics page ──────────
  // Run/stage stats come from the live runtime; agent/LLM token+cost from
  // Postgres; telemetry/Prometheus health from the telemetry adapter. Every
  // endpoint degrades to empty/null sections, so the page always renders.
  analytics: {
    summary: (days = 30) => apiFetch<AnalyticsSummary>(`/analytics/summary?days=${days}`),
    runsTimeseries: (days = 30) =>
      apiFetch<RunsTimeseriesPoint[]>(`/analytics/runs/timeseries?days=${days}`),
    stages: () => apiFetch<StageStat[]>("/analytics/stages"),
    agents: (days = 30) => apiFetch<AgentStat[]>(`/analytics/agents?days=${days}`),
    llmCost: (days = 30) => apiFetch<LLMCost>(`/analytics/llm/cost?days=${days}`),
    sreSummary: () => apiFetch<AnalyticsSRESummary>("/analytics/sre/summary"),
    telemetry: () => apiFetch<TelemetryHealth>("/analytics/telemetry"),
  },

  // ── Live preview: on-demand ephemeral preview environments ──────
  // preview_url is the unique forwarded host (preview-<id>.tesserix.app) —
  // the same URL the dashboard iframes AND opens in a new tab.
  preview: {
    start: (repo: string, ref = "main") =>
      apiFetch<PreviewSession>("/preview/start", {
        method: "POST",
        body: JSON.stringify({ repo, ref }),
      }),
    get: (id: string) => apiFetch<PreviewSession>(`/preview/${encodeURIComponent(id)}`),
    list: (mine = false) => apiFetch<PreviewSession[]>(`/preview${mine ? "?mine=true" : ""}`),
    stop: (id: string) =>
      apiFetch<{ stopped: string }>(`/preview/${encodeURIComponent(id)}/stop`, { method: "POST" }),
    // Diagnose + (by default) self-heal a preview's bring-up failures.
    verify: (id: string, heal = true) =>
      apiFetch<PreviewVerifyReport>(
        `/preview/${encodeURIComponent(id)}/verify?heal=${heal ? "true" : "false"}`,
        { method: "POST" },
      ),
  },
};

export interface PreviewSession {
  session_id: string;
  repo: string;
  ref: string;
  owner?: string;
  fe_url: string;
  preview_url: string;
  deployment?: string;
  status: "starting" | "running" | "failed" | "stopped" | "degraded";
}

export interface PreviewDiagnosis {
  container: string;
  class:
    | "healthy"
    | "pending"
    | "oom"
    | "port_mismatch"
    | "cors"
    | "install_failed"
    | "image_pull"
    | "crashloop"
    | "unknown";
  detail: string;
  action: { type: string; [k: string]: unknown };
  healable: boolean;
}

export interface PreviewVerifyReport {
  session_id?: string;
  name: string;
  status: "healthy" | "healing" | "degraded" | "pending" | "error";
  diagnoses: PreviewDiagnosis[];
  healed: string[];
  healable: boolean;
}

// ── SRE Studio types ──────────────────────────────────────────────
export interface SREDraft {
  id: string;
  kind: "blueprint" | "agent";
  name: string;
  yaml: string;
  description: string;
  status: "draft" | "published";
  created_by: string;
  created_at: string;
  updated_at: string;
  published_at?: string | null;
  last_dry_run_at?: string | null;
  dry_run_summary?: SREDryRunPreview | null;
}

export interface SREDryRunStageEvent {
  stage: string;
  phase: string;
  duration_ms?: number | null;
  message?: string;
  error?: string | null;
}

export interface SREDryRunPreview {
  dry_run: boolean;
  blueprint: string;
  task_id: string;
  state: string;
  stages_completed: string[];
  stages_failed: string[];
  stage_events: SREDryRunStageEvent[];
  outputs: Record<string, unknown>;
  note: string;
}

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

// ── Composer / teams types ────────────────────────────────────────────

export interface Team {
  id: string;
  name: string;
  org_id?: string;
  created_by?: string;
}

export interface CrewMemberRef {
  specialization: string;
  role_label?: string;
  allowed_tools?: string[];
}

export interface Crew {
  id: string;
  team_id: string;
  name: string;
  display_name?: string;
  description?: string;
  lead: string;
  members: CrewMemberRef[];
}

export interface ContextRef {
  type: "file" | "symbol" | "url";
  ref: string;
}

export interface ComposeRequest {
  intent: string;
  repo: string;
  blueprint?: string;
  trigger_type?: string;
  label?: string;
  team_id?: string;
  crew_id?: string;
  context_refs?: ContextRef[];
  attachments?: string[];
}

export interface DispatchResult {
  task_id: string;
  blueprint: string;
  state: string;
  team_id?: string;
}

// One server-sent stage/terminal/checkpoint event off /pipeline/events/stream.
export interface StreamEvent {
  timestamp: number;
  task_id: string;
  stage?: string;
  phase?: string;
  message?: string;
  error?: string | null;
  checkpoint?: string | null;
  type?: string;
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

export interface CreateRepoResult {
  ok: boolean;
  repo: string;
  html_url: string;
  default_branch: string;
  state: OnboardingStateValue;
  files_created: string[];
  files_skipped: Array<{ path: string; reason: string }>;
  branch_protected: boolean;
  adopted?: boolean;
  already_onboarded?: boolean;
}

// ── Settings types ─────────────────────────────────────────────────────
export interface SettingsField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  help: string;
  // For multi-provider connectors: the provider this field belongs to.
  // Empty = shown for all providers. The form shows only matching fields.
  provider?: string;
}

export interface SettingsConnectorSpec {
  key: string;
  label: string;
  family: string;
  provider_attr: string;
  providers: string[];
  description: string;
  multi: boolean;
  fields: SettingsField[];
}

export interface SettingsCatalog {
  connectors: SettingsConnectorSpec[];
  secrets_writable: boolean;
  has_db: boolean;
}

export interface SettingsConnector {
  scope: string;
  scope_id: string;
  connector_key: string;
  provider: string;
  instance_id: string;
  prefs: Record<string, unknown>;
  secrets_set: string[];
  enabled: boolean;
  updated_by: string;
  updated_at: string;
}

export interface SaveConnectorInput {
  scope: string;
  scope_id?: string;
  connector_key: string;
  provider: string;
  instance_id?: string;
  prefs?: Record<string, unknown>;
  secrets?: Record<string, string>;
}

// ── Analytics (GET /api/analytics/*) ──────────────────────────────────
export interface AnalyticsSummary {
  window_days: number;
  runs: {
    total: number;
    completed: number;
    failed: number;
    active: number;
    success_rate: number | null;
    avg_duration_ms: number | null;
  };
  by_state: Record<string, number>;
  by_blueprint: {
    blueprint: string;
    total: number;
    completed: number;
    failed: number;
    success_rate: number | null;
  }[];
}

export interface RunsTimeseriesPoint {
  date: string;
  total: number;
  completed: number;
  failed: number;
}

export interface StageStat {
  stage: string;
  runs: number;
  failures: number;
  avg_duration_ms: number | null;
}

export interface AgentStat {
  agent_name: string;
  executions: number;
  avg_duration_ms: number | null;
  tokens_input: number;
  tokens_output: number;
  total_cost_usd: number;
  failures: number;
}

export interface LLMCost {
  by_model: {
    provider: string;
    model: string;
    calls: number;
    tokens_input: number;
    tokens_output: number;
    cost_usd: number;
  }[];
  timeseries: { date: string; cost_usd: number; tokens: number }[];
}

export interface AnalyticsSRESummary {
  open_incidents?: number;
  critical_incidents?: number;
  total_apps?: number;
  latest_daily_cost?: number;
}

export interface TelemetryHealth {
  telemetry: {
    ok: boolean;
    provider: string;
    exporting?: boolean;
    endpoint?: string;
    detail?: string;
  };
  prometheus: { url: string; reachable: boolean; status?: number };
  metrics_enabled: boolean;
  provider: string;
}
