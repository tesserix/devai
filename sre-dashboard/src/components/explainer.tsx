"use client";

// A small, local "What this shows" explainer for the SRE dashboard.
//
// The SRE dashboard is a separate app from the ALM dashboard and must not
// depend on its GuidancePanel/help-content. This is a deliberately minimal,
// dismissible inline explainer in the same visual language (gray surfaces,
// rounded-lg, subtle border). It gives SRE users honest, fact-accurate context
// for each panel (incidents / health / cost / scans) so the numbers are not
// opaque.
//
// Copy is centralized in SRE_EXPLAINERS so it can't drift between panels and is
// easy to audit. Dismissal is per-id and persisted in localStorage, so the hint
// stays out of the way once a user has read it.

import { useEffect, useState } from "react";

export type ExplainerId =
  | "overview"
  | "incidents"
  | "apps"
  | "scans"
  | "costs"
  | "blueprints"
  | "schedules"
  | "sources";

interface ExplainerCopy {
  title: string;
  body: string;
  // Optional short bullets for the most data-dense panels.
  points?: string[];
}

export const SRE_EXPLAINERS: Record<ExplainerId, ExplainerCopy> = {
  overview: {
    title: "What this shows",
    body:
      "A roll-up of cluster health from the latest SRE scan. The SRE pipeline " +
      "(discover → monitor → correlate → respond → learn) maps your namespaces, " +
      "checks each app, and records what it found. Counts here reflect the most " +
      "recent scan, not a live cluster query — trigger a manual scan to refresh.",
    points: [
      "Open / Critical — incidents the pipeline filed and hasn't resolved.",
      "Daily Cost — the latest cost report for the cluster, if a provider is connected.",
    ],
  },
  incidents: {
    title: "What this shows",
    body:
      "Issues the SRE agents detected and filed during scans — performance, " +
      "availability, cost, capacity, security, or errors. Each carries a severity " +
      "and the agent that detected it. Some are linked to a GitHub issue the " +
      "pipeline opened. Select one to see the remediation actions recorded against it.",
    points: [
      "Severity drives color and ordering; critical bubbles to the top.",
      "These are scan-time findings — they don't update until the next scan.",
    ],
  },
  apps: {
    title: "What this shows",
    body:
      "Per-application reliability over a rolling window, derived from filed " +
      "incidents and health checks. The Discovery Agent auto-maps apps from your " +
      "namespaces — nothing to configure.",
    points: [
      "Incidents/30d — total filed; severe ones are highlighted in red.",
      "Health/7d — share of health checks that passed.",
      "MTTR — mean time to resolve, averaged over resolved incidents.",
    ],
  },
  scans: {
    title: "What this shows",
    body:
      "Every SRE scan run — manual or scheduled — with how many apps it checked " +
      "and how many incidents it found. A scan is one execution of the SRE " +
      "pipeline against the cluster. Running scans pulse; completed scans show a " +
      "green dot, failed ones red.",
  },
  costs: {
    title: "What this shows",
    body:
      "Cloud cost reports the Cost Analyzer agent collected for the cluster, with " +
      "a monthly forecast and the savings it estimates from its recommendations. " +
      "Figures come from the connected cost provider at scan time; with no provider " +
      "connected this stays empty.",
  },
  blueprints: {
    title: "What this shows",
    body:
      "SRE workflows the runtime can run — each is a fixed DAG of stages (the " +
      "topology doesn't change at runtime; stages can only be conditionally " +
      "skipped). 'Run now' executes one immediately; 'Flow' shows its stages by " +
      "dependency level; 'Schedule' sets a recurring cadence.",
  },
  schedules: {
    title: "What this shows",
    body:
      "Recurring cadences for published SRE blueprints. Toggle one to pause or " +
      "resume it without deleting it. 'Last run' reflects the most recent " +
      "execution the runtime recorded.",
  },
  sources: {
    title: "What this shows",
    body:
      "Observability backends the SRE runtime can pull metrics and logs from. " +
      "The in-cluster Prometheus is used by default; connect or edit additional " +
      "sources in DevAI → Settings → Observability. A green dot means reachable.",
  },
};

function storageKey(id: ExplainerId) {
  return `devai-sre-explainer-dismissed:${id}`;
}

export function Explainer({ id }: { id: ExplainerId }) {
  const copy = SRE_EXPLAINERS[id];
  // Start hidden to avoid a hydration flash, then reveal once we've read the
  // dismissed state on the client.
  const [show, setShow] = useState(false);

  useEffect(() => {
    try {
      setShow(window.localStorage.getItem(storageKey(id)) !== "1");
    } catch {
      setShow(true);
    }
  }, [id]);

  if (!copy || !show) return null;

  function dismiss() {
    try {
      window.localStorage.setItem(storageKey(id), "1");
    } catch {
      /* ignore */
    }
    setShow(false);
  }

  return (
    <div className="mb-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800/60 px-4 py-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-100 dark:bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 text-xs font-semibold"
        >
          i
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
            {copy.title}
          </p>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300 leading-relaxed">
            {copy.body}
          </p>
          {copy.points && copy.points.length > 0 && (
            <ul className="mt-2 space-y-1">
              {copy.points.map((pt) => (
                <li
                  key={pt}
                  className="flex gap-2 text-xs text-gray-500 dark:text-gray-400"
                >
                  <span aria-hidden className="text-gray-300 dark:text-gray-600">
                    •
                  </span>
                  <span>{pt}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <button
          type="button"
          onClick={dismiss}
          className="shrink-0 -mr-1 rounded px-1.5 py-0.5 text-xs text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          aria-label="Dismiss explainer"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
