const API = "/api";

// Guard so a burst of failing requests can't trigger multiple concurrent
// navigations to /login (which would also clobber the captured return_to).
let redirectingToLogin = false;

// Centralized 401 handling (DASH-11): when the SRE session has expired the
// API proxy returns 401. Instead of surfacing an opaque "401: ..." error in
// every panel, bounce the browser to /login?return_to=<current> so the user
// re-authenticates and lands back where they were. SSR-guarded and de-duped.
function handleUnauthorized(): never {
  if (typeof window !== "undefined" && !redirectingToLogin) {
    redirectingToLogin = true;
    const here = window.location.pathname + window.location.search;
    window.location.assign(`/login?return_to=${encodeURIComponent(here)}`);
  }
  throw new Error("Session expired");
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...opts?.headers },
  });
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export interface Incident {
  id: string;
  severity: string;
  category: string;
  title: string;
  description: string;
  status: string;
  scm_issue_number?: number;
  scm_repo?: string;
  assigned_to: string[];
  detected_by: string;
  created_at: string;
  resolved_at?: string;
  mttr_seconds?: number;
}

// A remediation action the SRE pipeline recorded against an incident.
// Shape mirrors the sre_remediations table (db/migrations/0002_sre_schema).
export interface Remediation {
  id: string;
  incident_id: string;
  action_type: string; // create_issue | create_pr | scale_up | restart_pod | rollback | alert
  description: string;
  target?: string | null; // deployment name, PR URL, issue URL
  payload?: Record<string, unknown>;
  status: string; // pending | executed | failed | rolled_back
  executed_at?: string | null;
  result?: string | null;
  created_at: string;
}

// Incident detail = the base incident plus its recorded remediations.
export interface IncidentDetail extends Incident {
  remediations: Remediation[];
}

// A single time-series metric sample. Shape mirrors the sre_metrics table.
export interface Metric {
  id: number;
  cluster_id: string;
  app_id?: string | null;
  metric_name: string; // cpu_usage | memory_usage | error_rate | latency_p99 | pod_restarts | request_rate
  metric_value: number;
  metric_unit?: string | null; // percent | bytes | ms | count | req/s
  labels?: Record<string, unknown>;
  recorded_at: string;
}

export interface ScanRun {
  id: string;
  cluster_id: string;
  trigger: string;
  status: string;
  incidents_found: number;
  apps_checked: number;
  agent_timings: Record<string, number>;
  started_at: string;
  completed_at?: string;
}

export interface ClusterHealth {
  cluster_id: string;
  cluster_name: string;
  total_apps: number;
  open_incidents: number;
  critical_incidents: number;
  latest_daily_cost?: number;
}

export interface AppReliability {
  app_id: string;
  name: string;
  namespace: string;
  repo: string;
  total_incidents_30d: number;
  severe_incidents_30d: number;
  avg_mttr_seconds?: number;
  health_pct_7d?: number;
}

export interface CostRecommendation {
  title?: string;
  recommendation?: string;
  savings_usd?: number;
}

export interface CostReport {
  id: number;
  cluster_id: string;
  report_date: string;
  provider: string;
  total_cost_usd: number;
  monthly_forecast?: number;
  breakdown?: { potential_savings_usd?: number };
  recommendations?: { items?: CostRecommendation[]; potential_savings_usd?: number };
}

export const sre = {
  triggerScan: (clusterId = "default") =>
    apiFetch<{ status: string }>("/scan/trigger", { method: "POST", body: JSON.stringify({ cluster_id: clusterId }) }),

  listScanRuns: (limit = 20) => apiFetch<ScanRun[]>(`/scan/runs?limit=${limit}`),

  listIncidents: (status = "open", limit = 50) =>
    apiFetch<Incident[]>(`/incidents?status=${status}&limit=${limit}`),

  getIncident: (id: string) => apiFetch<IncidentDetail>(`/incidents/${id}`),

  updateIncident: (id: string, data: { status?: string; resolution_note?: string }) =>
    apiFetch<{ status: string }>(`/incidents/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  clusterHealth: () => apiFetch<ClusterHealth[]>("/health"),

  listApps: () => apiFetch<AppReliability[]>("/apps"),

  metrics: (appId?: string, limit = 100) =>
    apiFetch<Metric[]>(`/metrics?${appId ? `app_id=${appId}&` : ""}limit=${limit}`),

  costs: (days = 30) => apiFetch<CostReport[]>(`/costs?days=${days}`),

  // ── Blueprints (published from DevAI SRE Studio + built-ins) ──────
  listBlueprints: () => apiFetch<SREBlueprint[]>("/blueprints"),

  blueprintGraph: (name: string) => apiFetch<BlueprintGraph>(`/blueprints/${encodeURIComponent(name)}/graph`),

  triggerBlueprint: (blueprint: string, clusterId = "default") =>
    apiFetch<{ status: string; blueprint: string; scan_id: string }>("/scan/trigger-blueprint", {
      method: "POST",
      body: JSON.stringify({ blueprint, cluster_id: clusterId }),
    }),

  scanFlow: (scanId: string) => apiFetch<ScanFlow>(`/scan/runs/${encodeURIComponent(scanId)}/flow`),

  // ── Schedules (cadence for published blueprints) ──────────────────
  listSchedules: () => apiFetch<SRESchedule[]>("/schedules"),

  createSchedule: (input: { blueprint: string; cron: string; cluster_id?: string; enabled?: boolean }) =>
    apiFetch<{ id: string }>("/schedules", { method: "POST", body: JSON.stringify(input) }),

  updateSchedule: (id: string, input: { cron?: string; enabled?: boolean }) =>
    apiFetch<{ updated: string }>(`/schedules/${id}`, { method: "PATCH", body: JSON.stringify(input) }),

  deleteSchedule: (id: string) => apiFetch<{ deleted: string }>(`/schedules/${id}`, { method: "DELETE" }),

  // ── Observability sources (read-only; configured in DevAI Settings) ──
  observabilitySources: () => apiFetch<ObservabilitySources>("/observability/sources"),
};

export interface ObservabilitySource {
  provider: string;
  ok: boolean;
  detail: string;
}

export interface ObservabilitySources {
  connected: string[];
  sources: ObservabilitySource[];
  error?: string;
}

export interface SREBlueprint {
  name: string;
  description: string;
  stage_count: number;
  kind: string;
  pattern: string;
  cadence: string;
  title: string;
}

export interface BlueprintGraphNode {
  name: string;
  stage: string;
  type: string;
  title: string;
  lane: string;
  depends_on: string[];
  parallel: boolean;
}

export interface BlueprintGraph {
  name: string;
  title: string;
  description: string;
  lanes: string[];
  nodes: BlueprintGraphNode[];
  levels: string[][];
}

export interface SRESchedule {
  id: string;
  blueprint: string;
  cron: string;
  cluster_id: string;
  enabled: boolean;
  last_run_at?: string | null;
  created_at?: string;
}

export interface ScanFlow {
  scan_id: string;
  status: string;
  blueprint?: string | null;
  trigger: string;
  incidents_found: number;
  agent_timings: Record<string, number>;
  graph: BlueprintGraph | null;
  started_at?: string | null;
  completed_at?: string | null;
}
