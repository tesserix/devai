"use client";

import { useCallback, useEffect, useState } from "react";
import { sre, type Incident, type ScanRun, type ClusterHealth, type AppReliability } from "@/lib/api";
import { IncidentFeed } from "@/components/incident-feed";
import { ClusterOverview } from "@/components/cluster-overview";
import { ScanHistory } from "@/components/scan-history";
import { SREChatPanel } from "@/components/sre-chat-panel";
import { clsx } from "clsx";

type Tab = "overview" | "incidents" | "apps" | "scans" | "costs" | "chat";

const NAV_ITEMS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "incidents", label: "Incidents" },
  { key: "apps", label: "Applications" },
  { key: "scans", label: "Scan History" },
  { key: "costs", label: "Cost Analysis" },
  { key: "chat", label: "Chat" },
];

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
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      {/* Sidebar */}
      <aside className="w-64 border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-emerald-600 flex items-center justify-center">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
              </svg>
            </div>
            <div>
              <h1 className="text-sm font-bold text-gray-900 dark:text-gray-100">DevAI SRE</h1>
              <p className="text-xs text-gray-400 dark:text-gray-500">Cluster Monitoring</p>
            </div>
          </div>
        </div>

        {/* Status Banner */}
        <div className={clsx(
          "mx-3 mt-3 p-3 rounded-lg border text-center",
          criticalCount > 0
            ? "border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10"
            : openCount > 0
            ? "border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10"
            : "border-emerald-200 dark:border-emerald-500/20 bg-emerald-50 dark:bg-emerald-500/5"
        )}>
          <p className={clsx(
            "text-2xl font-bold",
            criticalCount > 0
              ? "text-red-600 dark:text-red-400"
              : openCount > 0
              ? "text-amber-600 dark:text-amber-400"
              : "text-emerald-600 dark:text-emerald-400"
          )}>
            {criticalCount > 0 ? criticalCount : openCount > 0 ? openCount : "0"}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            {criticalCount > 0 ? "Critical Incidents" : openCount > 0 ? "Open Incidents" : "All Clear"}
          </p>
        </div>

        {/* Navigation */}
        <nav className="flex-1 p-3 space-y-0.5">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.key}
              onClick={() => setTab(item.key)}
              className={clsx(
                "w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm font-medium transition-colors",
                tab === item.key
                  ? "bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700/50"
              )}
            >
              <span>{item.label}</span>
              {item.key === "incidents" && openCount > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded-full bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                  {openCount}
                </span>
              )}
              {item.key === "apps" && apps.length > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded-full bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                  {apps.length}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* Trigger Scan */}
        <div className="p-3 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={() => sre.triggerScan().then(fetchAll)}
            className="w-full px-3 py-2 text-xs font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 transition-colors"
          >
            Trigger Manual Scan
          </button>
          <div className="flex items-center justify-between mt-2">
            <div className="flex items-center gap-2 text-xs text-gray-400 dark:text-gray-500">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              Auto-scanning every 5m
            </div>
            <a
              href="/bff/logout"
              className="text-xs text-gray-400 dark:text-gray-500 hover:text-red-500 dark:hover:text-red-400 transition-colors"
            >
              Logout
            </a>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto">
        {/* Header */}
        <header className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {tab === "overview" && "Cluster Overview"}
            {tab === "incidents" && "Incidents"}
            {tab === "apps" && "Application Reliability"}
            {tab === "scans" && "Scan History"}
            {tab === "costs" && "Cost Analysis"}
            {tab === "chat" && "SRE Assistant"}
          </h2>
        </header>

        <div className="p-6">
          {loading ? (
            <div className="text-center py-16 text-gray-400 dark:text-gray-500">Loading...</div>
          ) : (
            <>
              {tab === "overview" && (
                <div className="space-y-8">
                  <ClusterOverview clusters={clusters} />

                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div>
                      <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
                        Recent Incidents
                      </h3>
                      <IncidentFeed incidents={incidents.slice(0, 5)} onSelect={setSelectedIncident} />
                    </div>
                    <div>
                      <h3 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
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
                      className="flex items-center justify-between p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
                    >
                      <div>
                        <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{app.name}</h4>
                        <p className="text-xs text-gray-400 dark:text-gray-500">{app.namespace} — {app.repo}</p>
                      </div>
                      <div className="flex items-center gap-6 text-xs">
                        <div className="text-center">
                          <p className={clsx(
                            "text-lg font-bold",
                            app.severe_incidents_30d > 0
                              ? "text-red-600 dark:text-red-400"
                              : "text-gray-900 dark:text-gray-100"
                          )}>{app.total_incidents_30d}</p>
                          <p className="text-xs text-gray-400 dark:text-gray-500">Incidents/30d</p>
                        </div>
                        <div className="text-center">
                          <p className="text-lg font-bold text-gray-900 dark:text-gray-100">
                            {app.health_pct_7d !== null ? `${app.health_pct_7d?.toFixed(0)}%` : "—"}
                          </p>
                          <p className="text-xs text-gray-400 dark:text-gray-500">Health/7d</p>
                        </div>
                        <div className="text-center">
                          <p className="text-lg font-bold text-gray-900 dark:text-gray-100">
                            {app.avg_mttr_seconds ? `${(app.avg_mttr_seconds / 60).toFixed(0)}m` : "—"}
                          </p>
                          <p className="text-xs text-gray-400 dark:text-gray-500">MTTR</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {tab === "scans" && <ScanHistory runs={scans} />}

              {tab === "costs" && (
                <div className="text-center py-16 text-gray-400 dark:text-gray-500">
                  <p className="text-lg">Cost analysis loading from GCP billing...</p>
                  <p className="text-sm mt-1">Data refreshes daily</p>
                </div>
              )}

              {tab === "chat" && <SREChatPanel />}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
