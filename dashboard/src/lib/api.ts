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
};
