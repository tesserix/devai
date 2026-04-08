"use client";

import { useCallback, useEffect, useState } from "react";
import { sre, type Incident, type ScanRun, type ClusterHealth, type AppReliability } from "@/lib/api";
import { IncidentFeed } from "@/components/incident-feed";
import { ClusterOverview } from "@/components/cluster-overview";
import { ScanHistory } from "@/components/scan-history";
import { clsx } from "clsx";

type Tab = "overview" | "incidents" | "apps" | "scans" | "costs";

export default function SREDashboard() {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [clusters, setClusters] = useState<ClusterHealth[]>([]);
  const [apps, setApps] = useState<AppReliability[]>([]);
  const [scans, setScans] = useState<ScanRun[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [selectedIncident, setSelectedIncident] = useState<string>();
  const [loading, setLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    try {
      const [inc, cl, ap, sc] = await Promise.all([
        sre.listIncidents("open").catch(() => []),
        sre.clusterHealth().catch(() => []),
        sre.listApps().catch(() => []),
        sre.listScanRuns().catch(() => []),
      ]);
      setIncidents(inc);
      setClusters(cl);
      setApps(ap);
      setScans(sc);
    } catch {
      // API not available
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 10000);
    return () => clearInterval(interval);
  }, [fetchAll]);

  const criticalCount = incidents.filter((i) => i.severity === "critical").length;
  const openCount = incidents.length;

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-64 border-r border-[var(--border-primary)] bg-[var(--bg-secondary)] flex flex-col">
        <div className="p-4 border-b border-[var(--border-primary)]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-emerald-500 to-cyan-600 flex items-center justify-center text-white text-sm font-bold">
              S
            </div>
            <div>
              <h1 className="text-sm font-bold text-[var(--text-primary)]">DevAI SRE</h1>
              <p className="text-[10px] text-[var(--text-muted)]">Cluster Monitoring</p>
            </div>
          </div>
        </div>

        {/* Status Banner */}
        <div className={clsx(
          "mx-3 mt-3 p-3 rounded-lg border text-center",
          criticalCount > 0 ? "border-red-500/30 bg-red-500/10" :
          openCount > 0 ? "border-amber-500/30 bg-amber-500/10" :
          "border-green-500/20 bg-green-500/5"
        )}>
          <p className={clsx(
            "text-2xl font-bold",
            criticalCount > 0 ? "text-red-400" : openCount > 0 ? "text-amber-400" : "text-green-400"
          )}>
            {criticalCount > 0 ? criticalCount : openCount > 0 ? openCount : "0"}
          </p>
          <p className="text-[10px] text-[var(--text-muted)] mt-0.5">
            {criticalCount > 0 ? "Critical Incidents" : openCount > 0 ? "Open Incidents" : "All Clear"}
          </p>
        </div>

        {/* Navigation */}
        <nav className="flex-1 p-3 space-y-1">
          {([
            { key: "overview", label: "Overview", icon: "📊" },
            { key: "incidents", label: "Incidents", icon: "🔥", badge: openCount },
            { key: "apps", label: "Applications", icon: "📦", badge: apps.length },
            { key: "scans", label: "Scan History", icon: "🔄" },
            { key: "costs", label: "Cost Analysis", icon: "💰" },
          ] as const).map((item) => (
            <button
              key={item.key}
              onClick={() => setTab(item.key)}
              className={clsx(
                "w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition-colors",
                tab === item.key
                  ? "bg-[var(--bg-tertiary)] text-[var(--text-primary)]"
                  : "text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]/50"
              )}
            >
              <span>{item.icon} {item.label}</span>
              {"badge" in item && item.badge! > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--bg-elevated)] text-[var(--text-secondary)]">
                  {item.badge}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* Trigger Scan */}
        <div className="p-3 border-t border-[var(--border-primary)]">
          <button
            onClick={() => sre.triggerScan().then(fetchAll)}
            className="w-full px-3 py-2 text-xs font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors"
          >
            Trigger Manual Scan
          </button>
          <div className="flex items-center justify-between mt-2">
            <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
              <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
              Auto-scanning every 5m
            </div>
            <a
              href="/bff/logout"
              className="text-[10px] text-[var(--text-muted)] hover:text-red-400 transition-colors"
            >
              Logout
            </a>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto">
        {/* Header */}
        <header className="border-b border-[var(--border-primary)] bg-[var(--bg-secondary)] px-6 py-4">
          <h2 className="text-lg font-semibold text-[var(--text-primary)]">
            {tab === "overview" && "Cluster Overview"}
            {tab === "incidents" && "Incidents"}
            {tab === "apps" && "Application Reliability"}
            {tab === "scans" && "Scan History"}
            {tab === "costs" && "Cost Analysis"}
          </h2>
        </header>

        <div className="p-6">
          {loading ? (
            <div className="text-center py-16 text-[var(--text-muted)]">Loading...</div>
          ) : (
            <>
              {tab === "overview" && (
                <div className="space-y-8">
                  <ClusterOverview clusters={clusters} />

                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div>
                      <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
                        Recent Incidents
                      </h3>
                      <IncidentFeed incidents={incidents.slice(0, 5)} onSelect={setSelectedIncident} />
                    </div>
                    <div>
                      <h3 className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-3">
                        Recent Scans
                      </h3>
                      <ScanHistory runs={scans.slice(0, 8)} />
                    </div>
                  </div>
                </div>
              )}

              {tab === "incidents" && (
                <IncidentFeed incidents={incidents} onSelect={setSelectedIncident} />
              )}

              {tab === "apps" && (
                <div className="space-y-2">
                  {apps.map((app) => (
                    <div
                      key={app.app_id}
                      className="flex items-center justify-between p-4 rounded-lg border border-[var(--border-primary)] bg-[var(--bg-secondary)]"
                    >
                      <div>
                        <h4 className="text-sm font-semibold text-[var(--text-primary)]">{app.name}</h4>
                        <p className="text-xs text-[var(--text-muted)]">{app.namespace} — {app.repo}</p>
                      </div>
                      <div className="flex items-center gap-6 text-xs">
                        <div className="text-center">
                          <p className={clsx("text-lg font-bold",
                            app.severe_incidents_30d > 0 ? "text-red-400" : "text-[var(--text-primary)]"
                          )}>{app.total_incidents_30d}</p>
                          <p className="text-[10px] text-[var(--text-muted)]">Incidents/30d</p>
                        </div>
                        <div className="text-center">
                          <p className="text-lg font-bold text-[var(--text-primary)]">
                            {app.health_pct_7d !== null ? `${app.health_pct_7d?.toFixed(0)}%` : "—"}
                          </p>
                          <p className="text-[10px] text-[var(--text-muted)]">Health/7d</p>
                        </div>
                        <div className="text-center">
                          <p className="text-lg font-bold text-[var(--text-primary)]">
                            {app.avg_mttr_seconds ? `${(app.avg_mttr_seconds / 60).toFixed(0)}m` : "—"}
                          </p>
                          <p className="text-[10px] text-[var(--text-muted)]">MTTR</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {tab === "scans" && <ScanHistory runs={scans} />}

              {tab === "costs" && (
                <div className="text-center py-16 text-[var(--text-muted)]">
                  <p className="text-lg">Cost analysis loading from GCP billing...</p>
                  <p className="text-sm mt-1">Data refreshes daily</p>
                </div>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
