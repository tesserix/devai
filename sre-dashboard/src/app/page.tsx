"use client";

import { useCallback, useEffect, useState } from "react";
import {
  sre,
  type Incident,
  type ScanRun,
  type ClusterHealth,
  type AppReliability,
  type CostReport,
} from "@/lib/api";
import { IncidentFeed } from "@/components/incident-feed";
import { ClusterOverview } from "@/components/cluster-overview";
import { ScanHistory } from "@/components/scan-history";
import { SREChatPanel } from "@/components/sre-chat-panel";
import { RuntimePanel } from "@/components/runtime-panel";
import { ToastProvider, useToast } from "@/components/toast";
import { Explainer } from "@/components/explainer";
import { clsx } from "clsx";

type Tab =
  | "overview"
  | "incidents"
  | "apps"
  | "scans"
  | "blueprints"
  | "schedules"
  | "sources"
  | "costs"
  | "chat";

const NAV_ITEMS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "incidents", label: "Incidents" },
  { key: "apps", label: "Applications" },
  { key: "scans", label: "Scan History" },
  { key: "blueprints", label: "Blueprints" },
  { key: "schedules", label: "Schedules" },
  { key: "sources", label: "Sources" },
  { key: "costs", label: "Cost Analysis" },
  { key: "chat", label: "Chat" },
];

export default function SREDashboard() {
  // ToastProvider must wrap the component that calls useToast (DASH-11).
  return (
    <ToastProvider>
      <SREDashboardInner />
    </ToastProvider>
  );
}

function SREDashboardInner() {
  const toast = useToast();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [clusters, setClusters] = useState<ClusterHealth[]>([]);
  const [apps, setApps] = useState<AppReliability[]>([]);
  const [scans, setScans] = useState<ScanRun[]>([]);
  const [costs, setCosts] = useState<CostReport[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [selectedIncident, setSelectedIncident] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  // Mobile nav drawer (the sidebar is an inline rail on md+).
  const [navOpen, setNavOpen] = useState(false);

  // `selectedIncident` is wired to the incident feed for future drill-in; mark
  // it referenced so the lint/type pass doesn't flag the setter-only usage.
  void selectedIncident;

  // Selecting a tab closes the mobile drawer.
  useEffect(() => {
    setNavOpen(false);
  }, [tab]);

  const fetchAll = useCallback(async () => {
    try {
      const [inc, cl, ap, sc, co] = await Promise.all([
        sre.listIncidents("open").catch(() => []),
        sre.clusterHealth().catch(() => []),
        sre.listApps().catch(() => []),
        sre.listScanRuns().catch(() => []),
        sre.costs().catch(() => []),
      ]);
      setIncidents(inc);
      setClusters(cl);
      setApps(ap);
      setScans(sc);
      setCosts(co);
    } catch {
      // API not available
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll every 10s, but ONLY while the tab is visible (DASH-11). Polling a
  // hidden tab wastes the SRE API's kubectl-backed budget and battery; we also
  // refresh immediately on becoming visible so the view isn't stale.
  useEffect(() => {
    fetchAll();
    let interval: ReturnType<typeof setInterval> | undefined;

    const start = () => {
      if (interval) return;
      interval = setInterval(() => {
        if (document.visibilityState === "visible") fetchAll();
      }, 10000);
    };
    const stop = () => {
      if (interval) {
        clearInterval(interval);
        interval = undefined;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        fetchAll();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fetchAll]);

  // Trigger a manual scan with explicit busy state + error toast (DASH-11).
  // Previously this was a bare `sre.triggerScan().then(fetchAll)` whose
  // rejection was unhandled, so a failed trigger looked like a no-op.
  const triggerScan = useCallback(async () => {
    if (scanning) return;
    setScanning(true);
    try {
      await sre.triggerScan();
      toast.success("Scan triggered — results will appear shortly.");
      await fetchAll();
    } catch (e) {
      toast.error(`Could not trigger scan: ${errMsg(e)}`);
    } finally {
      setScanning(false);
    }
  }, [scanning, toast, fetchAll]);

  const criticalCount = incidents.filter((i) => i.severity === "critical").length;
  const openCount = incidents.length;

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      {/* Backdrop behind the mobile drawer. */}
      {navOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/45 backdrop-blur-[2px] md:hidden"
          onClick={() => setNavOpen(false)}
          aria-hidden
        />
      )}
      {/* Sidebar — fixed slide-in drawer below md, static rail on md+. */}
      <aside
        className={clsx(
          "w-64 border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex flex-col",
          "fixed inset-y-0 left-0 z-40 transition-transform duration-200 ease-out md:static md:z-auto md:translate-x-0",
          navOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
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
            onClick={triggerScan}
            disabled={scanning}
            className="w-full px-3 py-2 text-xs font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 transition-colors disabled:opacity-60 disabled:cursor-wait"
          >
            {scanning ? "Triggering scan…" : "Trigger Manual Scan"}
          </button>
          <div className="flex items-center justify-between mt-2">
            <div className="flex items-center gap-2 text-xs text-gray-400 dark:text-gray-500">
              <span className="w-1.5 h-1.5 rounded-full bg-gray-400" />
              Manual scan only
            </div>
            <a
              href="/auth/logout"
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
        <header className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 sm:px-6 py-3.5 sm:py-4 flex items-center gap-2.5 sticky top-0 z-20">
          {/* Hamburger opens the nav drawer (mobile only). */}
          <button
            type="button"
            onClick={() => setNavOpen(true)}
            className="md:hidden -ml-1 p-2 rounded-md text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
            aria-label="Open menu"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
          <h2 className="text-base sm:text-lg font-semibold text-gray-900 dark:text-gray-100">
            {tab === "overview" && "Cluster Overview"}
            {tab === "incidents" && "Incidents"}
            {tab === "apps" && "Application Reliability"}
            {tab === "scans" && "Scan History"}
            {tab === "blueprints" && "Blueprints"}
            {tab === "schedules" && "Schedules"}
            {tab === "sources" && "Observability Sources"}
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
                <>
                  <Explainer id="overview" />
                  {clusters.length === 0 ? (
                  <EmptyState
                    title="Waiting for first scan"
                    body="The SRE service is up but no scan has populated cluster health yet. Click 'Trigger Manual Scan' in the sidebar."
                  />
                ) : (
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
                </>
              )}

              {tab === "incidents" && (
                <>
                  <Explainer id="incidents" />
                  {incidents.length === 0 ? (
                  <EmptyState
                    title="No open incidents"
                    body="The SRE pipeline hasn't detected any actionable issues. Trigger a manual scan to refresh."
                  />
                ) : (
                  <IncidentFeed incidents={incidents} onSelect={setSelectedIncident} />
                )}
                </>
              )}

              {tab === "apps" && (
                <>
                  <Explainer id="apps" />
                  {apps.length === 0 ? (
                  <EmptyState
                    title="No applications discovered yet"
                    body="The Discovery Agent hasn't run yet, or the cluster has no namespaces. Trigger a manual scan from the sidebar."
                  />
                ) : (
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
                </>
              )}

              {tab === "scans" && (
                <>
                  <Explainer id="scans" />
                  {scans.length === 0 ? (
                  <EmptyState
                    title="No scans recorded yet"
                    body="Trigger your first manual scan to populate the SRE database."
                  />
                ) : (
                  <ScanHistory runs={scans} />
                )}
                </>
              )}

              {tab === "costs" && (
                <>
                  <Explainer id="costs" />
                  {costs.length === 0 ? (
                  <div className="text-center py-16 text-gray-400 dark:text-gray-500">
                    <p className="text-lg">No cost data yet</p>
                    <p className="text-sm mt-1">Trigger a manual scan to populate cost analysis</p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    {costs.map((c) => {
                      const items = c.recommendations?.items ?? [];
                      const savings = c.recommendations?.potential_savings_usd ?? 0;
                      return (
                        <div
                          key={c.id}
                          className="p-5 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
                        >
                          <div className="flex items-center justify-between">
                            <div>
                              <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                                {c.cluster_id}
                              </h3>
                              <p className="text-xs text-gray-400 dark:text-gray-500">
                                {c.report_date} · {c.provider.toUpperCase()}
                              </p>
                            </div>
                            <div className="grid grid-cols-3 gap-6 text-right">
                              <div>
                                <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                                  ${c.total_cost_usd.toFixed(2)}
                                </p>
                                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Daily</p>
                              </div>
                              <div>
                                <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                                  {c.monthly_forecast ? `$${c.monthly_forecast.toFixed(0)}` : "—"}
                                </p>
                                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Monthly</p>
                              </div>
                              <div>
                                <p className="text-2xl font-bold text-emerald-600 dark:text-emerald-400">
                                  ${savings.toFixed(0)}
                                </p>
                                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">Savings</p>
                              </div>
                            </div>
                          </div>

                          {items.length > 0 && (
                            <div className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-3 space-y-2">
                              {items.map((rec, i) => (
                                <div key={i} className="flex items-start justify-between text-xs">
                                  <div className="flex-1 pr-4">
                                    <p className="font-medium text-gray-700 dark:text-gray-200">
                                      {rec.title}
                                    </p>
                                    {rec.recommendation && (
                                      <p className="text-gray-500 dark:text-gray-400 mt-0.5">
                                        {rec.recommendation}
                                      </p>
                                    )}
                                  </div>
                                  {rec.savings_usd ? (
                                    <span className="text-emerald-600 dark:text-emerald-400 font-mono">
                                      ${Number(rec.savings_usd).toFixed(0)}/mo
                                    </span>
                                  ) : null}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
                </>
              )}

              {tab === "blueprints" && <RuntimePanel mode="blueprints" />}
              {tab === "schedules" && <RuntimePanel mode="schedules" />}
              {tab === "sources" && <RuntimePanel mode="sources" />}

              {tab === "chat" && <SREChatPanel />}
            </>
          )}
        </div>
      </main>
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="text-center py-16">
      <p className="text-base font-semibold text-gray-700 dark:text-gray-200">{title}</p>
      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1.5 max-w-md mx-auto">{body}</p>
    </div>
  );
}

// Normalize an unknown thrown value to a readable string for toasts/errors.
function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
