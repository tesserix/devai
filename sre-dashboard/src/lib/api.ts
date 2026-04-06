const API = "/api";

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...opts?.headers },
  });
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

export const sre = {
  triggerScan: (clusterId = "default") =>
    apiFetch<{ status: string }>("/scan/trigger", { method: "POST", body: JSON.stringify({ cluster_id: clusterId }) }),

  listScanRuns: (limit = 20) => apiFetch<ScanRun[]>(`/scan/runs?limit=${limit}`),

  listIncidents: (status = "open", limit = 50) =>
    apiFetch<Incident[]>(`/incidents?status=${status}&limit=${limit}`),

  getIncident: (id: string) => apiFetch<Incident & { remediations: any[] }>(`/incidents/${id}`),

  updateIncident: (id: string, data: { status?: string; resolution_note?: string }) =>
    apiFetch<{ status: string }>(`/incidents/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  clusterHealth: () => apiFetch<ClusterHealth[]>("/health"),

  listApps: () => apiFetch<AppReliability[]>("/apps"),

  metrics: (appId?: string, limit = 100) =>
    apiFetch<any[]>(`/metrics?${appId ? `app_id=${appId}&` : ""}limit=${limit}`),

  costs: (days = 30) => apiFetch<any[]>(`/costs?days=${days}`),
};
