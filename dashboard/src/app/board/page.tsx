"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  GitBranch,
  Loader2,
  RefreshCw,
  Rocket,
  Search,
  Truck,
} from "lucide-react";

import { Breadcrumbs } from "@/components/breadcrumbs";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";
import { EmptyState } from "@/components/empty-state";

/**
 * Board — the Kanban over GitHub issues in the active repo.
 *
 * (Moved here from /workflows in the DASH-5 IA disambiguation: "Workflows" is
 * now the lifecycle home for blueprints; this issue board is its own concept
 * and does NOT execute anything — it's for triage and tracking.)
 *
 * Five swimlanes mirror the lifecycle:
 *
 *   QUEUED       not started; ready to be picked
 *   IN-PROGRESS  agent has the issue
 *   REVIEW       PR opened, reviewer feedback loop
 *   DEPLOYED     merged + deployed to staging / preview
 *   SHIPPED      live in production
 *
 * Lane membership is label-driven (see _LANE_LABELS in
 * src/devai/scm/routes.py). The dashboard reads
 * GET /api/scm/issues?repo=<owner>/<name> and renders one column per lane.
 *
 * Repo selection persists in localStorage("devai-board-repo").
 */

type Lane = "queued" | "in_progress" | "review" | "deployed" | "shipped";

type Issue = {
  number: number;
  title: string;
  state: "open" | "closed";
  labels: string[];
  updated_at: string;
  html_url: string;
  lane: Lane;
  assignee: string;
};

type Lanes = Record<Lane, Issue[]>;

type Repo = {
  full_name: string;
  name: string;
  description: string;
  private: boolean;
  default_branch: string;
  html_url: string;
  pushed_at: string;
};

const LANE_ORDER: Lane[] = ["queued", "in_progress", "review", "deployed", "shipped"];

const LANE_META: Record<Lane, { label: string; Icon: typeof CircleDashed; tone: string }> = {
  queued:      { label: "Queued",       Icon: CircleDashed, tone: "var(--ink-muted)" },
  in_progress: { label: "In progress",  Icon: Loader2,      tone: "var(--info)" },
  review:      { label: "Review",       Icon: GitBranch,    tone: "var(--warn)" },
  deployed:    { label: "Deployed",     Icon: Truck,        tone: "var(--accent)" },
  shipped:     { label: "Shipped",      Icon: Rocket,       tone: "var(--ok)" },
};

// Legacy key from when this lived at /workflows — read it once so a returning
// user keeps their repo selection across the move.
const REPO_KEY = "devai-board-repo";
const LEGACY_REPO_KEY = "devai-workflows-repo";

export default function BoardPage() {
  const [repoQuery, setRepoQuery] = useState("");
  const [repoList, setRepoList] = useState<Repo[]>([]);
  const [repoOpen, setRepoOpen] = useState(false);
  const [repoLoading, setRepoLoading] = useState(false);
  const [activeRepo, setActiveRepoState] = useState<string>("");
  const [lanes, setLanes] = useState<Lanes | null>(null);
  const [lanesLoading, setLanesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const repoBoxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const stored =
      window.localStorage.getItem(REPO_KEY) ?? window.localStorage.getItem(LEGACY_REPO_KEY);
    if (stored) setActiveRepoState(stored);
  }, []);

  function setActiveRepo(full: string) {
    setActiveRepoState(full);
    window.localStorage.setItem(REPO_KEY, full);
    setRepoOpen(false);
    setRepoQuery("");
    // Fire-and-forget repo scan so the memory adapter has a fresh profile
    // (tech, frameworks, package managers) before the next pipeline run.
    const [owner, name] = full.split("/", 2);
    if (owner && name) {
      fetch(
        `/api/scm/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/scan`,
        { method: "POST" }
      ).catch(() => undefined);
    }
  }

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (repoBoxRef.current && !repoBoxRef.current.contains(e.target as Node)) {
        setRepoOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  useEffect(() => {
    if (!repoOpen) return;
    let cancelled = false;
    setRepoLoading(true);
    // Only enrolled repos (those with .platform/devai.yaml on the default
    // branch) belong on the Board. Anything else should go through ⌘K @ first.
    const params = new URLSearchParams({ initialised: "true" });
    if (repoQuery) params.set("q", repoQuery);
    const url = `/api/scm/repos?${params.toString()}`;
    fetch(url)
      .then(async (r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: Repo[]) => {
        if (!cancelled) setRepoList(data);
      })
      .catch(() => {
        if (!cancelled) setRepoList([]);
      })
      .finally(() => {
        if (!cancelled) setRepoLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repoQuery, repoOpen]);

  const fetchLanes = useCallback(async (repoFull: string) => {
    if (!repoFull) return;
    setLanesLoading(true);
    setError(null);
    try {
      const r = await fetch(`/api/scm/issues?repo=${encodeURIComponent(repoFull)}&state=all`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setLanes(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLanes(null);
    } finally {
      setLanesLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeRepo) fetchLanes(activeRepo);
  }, [activeRepo, fetchLanes]);

  const totals = useMemo(() => {
    if (!lanes) return null;
    return LANE_ORDER.reduce<Record<Lane, number>>(
      (acc, l) => {
        acc[l] = (lanes[l] || []).length;
        return acc;
      },
      { queued: 0, in_progress: 0, review: 0, deployed: 0, shipped: 0 },
    );
  }, [lanes]);

  return (
    <div className="p-7 space-y-5 min-h-full">
      <Breadcrumbs items={[{ label: "Fleet", href: "/" }, { label: "Board" }]} />

      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Tracking</div>
          <h1 className="font-serif text-2xl font-medium mt-1" style={{ color: "var(--ink-strong)" }}>
            Board
            <GuidanceInfo id="board" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
            GitHub issues from the selected repository, grouped by lane —{" "}
            <span style={{ color: "var(--ink-muted)" }}>queued → in progress → review → deployed → shipped</span>.
            This board tracks work; to automate it, run a workflow.
          </p>
        </div>
        <button
          onClick={() => activeRepo && fetchLanes(activeRepo)}
          disabled={!activeRepo || lanesLoading}
          className="btn-secondary"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${lanesLoading ? "animate-spin" : ""}`} /> Refresh
        </button>
      </header>

      <GuidancePanel id="board" />

      {/* Repo picker. Click to open; type to filter. Selection persists. */}
      <div ref={repoBoxRef} className="relative max-w-md">
        <button
          type="button"
          onClick={() => setRepoOpen((v) => !v)}
          className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-md text-sm transition-colors"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            color: activeRepo ? "var(--ink)" : "var(--ink-muted)",
          }}
        >
          <span className="flex items-center gap-2 min-w-0">
            <Search className="w-3.5 h-3.5" style={{ color: "var(--ink-muted)" }} />
            <span className="truncate font-mono">{activeRepo || "Select a repository…"}</span>
          </span>
          <span className="label-eyebrow">change</span>
        </button>

        {repoOpen && (
          <div
            className="absolute z-10 mt-1 w-full max-h-[60vh] overflow-y-auto panel-raised"
            style={{ background: "var(--surface-raised)" }}
          >
            <div className="p-2 border-b" style={{ borderColor: "var(--border-subtle)" }}>
              <input
                autoFocus
                value={repoQuery}
                onChange={(e) => setRepoQuery(e.target.value)}
                placeholder="Search repositories…"
                className="field w-full"
              />
            </div>
            {repoLoading ? (
              <div className="px-3 py-4 text-sm" style={{ color: "var(--ink-muted)" }}>
                Loading…
              </div>
            ) : repoList.length === 0 ? (
              <div className="px-3 py-4 text-sm" style={{ color: "var(--ink-muted)" }}>
                No enrolled repositories. Press{" "}
                <kbd
                  className="font-mono text-[10px] px-1 py-0.5 rounded border"
                  style={{
                    borderColor: "var(--border-subtle)",
                    background: "var(--surface-muted)",
                    color: "var(--ink)",
                  }}
                >
                  ⌘K
                </kbd>{" "}
                then type <code>@</code> to enrol one.
              </div>
            ) : (
              <ul className="divide-y" style={{ borderColor: "var(--border-subtle)" }}>
                {repoList.map((r) => (
                  <li key={r.full_name}>
                    <button
                      onClick={() => setActiveRepo(r.full_name)}
                      className="w-full text-left px-3 py-2 hover:bg-[var(--surface-hover)] transition-colors"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="font-mono text-sm" style={{ color: "var(--ink-strong)" }}>
                          {r.full_name}
                        </span>
                        {r.private && <span className="pill-muted pill">private</span>}
                      </div>
                      {r.description && (
                        <p className="text-xs mt-0.5" style={{ color: "var(--ink-soft)" }}>
                          {r.description}
                        </p>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* Lane summary chips */}
      {totals && (
        <div className="flex items-center gap-2 flex-wrap">
          {LANE_ORDER.map((l) => {
            const { label, Icon, tone } = LANE_META[l];
            return (
              <span
                key={l}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs"
                style={{
                  background: "var(--surface-muted)",
                  border: "1px solid var(--border-subtle)",
                  color: "var(--ink-soft)",
                }}
              >
                <Icon className="w-3 h-3" style={{ color: tone }} />
                <span>{label}</span>
                <span className="font-mono tabular-nums" style={{ color: "var(--ink-strong)" }}>
                  {totals[l]}
                </span>
              </span>
            );
          })}
        </div>
      )}

      {error && (
        <div
          className="rounded-md px-3 py-2 text-sm font-mono"
          style={{
            background: "var(--error-soft-bg)",
            color: "var(--error-ink)",
            border: "1px solid var(--error-soft-bd)",
          }}
        >
          {error}
        </div>
      )}

      {!activeRepo ? (
        <div className="panel">
          <EmptyState
            Icon={Search}
            title="Pick a repository"
            description="Choose an enrolled repository above to load its issue board. Need to add one? Press ⌘K and type @ to enrol it."
            secondary={{ label: "Run a workflow", href: "/workflows" }}
          />
        </div>
      ) : (
        <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
          {LANE_ORDER.map((lane) => (
            <Column key={lane} lane={lane} issues={(lanes && lanes[lane]) || []} loading={lanesLoading} />
          ))}
        </section>
      )}
    </div>
  );
}

function Column({ lane, issues, loading }: { lane: Lane; issues: Issue[]; loading: boolean }) {
  const { label, Icon, tone } = LANE_META[lane];
  return (
    <div className="flex flex-col min-w-0">
      <header
        className="flex items-center justify-between px-3 py-2 rounded-t-md border"
        style={{ background: "var(--surface-muted)", borderColor: "var(--border-subtle)" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <Icon
            className={`w-3.5 h-3.5 shrink-0 ${lane === "in_progress" ? "animate-spin" : ""}`}
            style={{ color: tone }}
          />
          <span className="label-eyebrow !text-[10px]">{label}</span>
        </div>
        <span className="font-mono text-xs tabular-nums" style={{ color: "var(--ink-strong)" }}>
          {issues.length}
        </span>
      </header>
      <div
        className="flex-1 min-h-[200px] rounded-b-md border border-t-0 p-2 space-y-2"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        {loading && issues.length === 0 ? (
          <div className="text-xs px-1 py-2" style={{ color: "var(--ink-muted)" }}>
            Loading…
          </div>
        ) : issues.length === 0 ? (
          <div className="text-xs px-1 py-2" style={{ color: "var(--ink-muted)" }}>
            Nothing in this lane.
          </div>
        ) : (
          issues.map((i) => <Card key={i.number} issue={i} />)
        )}
      </div>
    </div>
  );
}

function Card({ issue }: { issue: Issue }) {
  return (
    <a
      href={issue.html_url}
      target="_blank"
      rel="noreferrer"
      className="block rounded-md p-2.5 transition-colors group"
      style={{ background: "var(--surface)", border: "1px solid var(--border-subtle)" }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--border)")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border-subtle)")}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono text-xs" style={{ color: "var(--ink-muted)" }}>
          #{issue.number}
        </span>
        <ExternalLink
          className="w-3 h-3 opacity-0 group-hover:opacity-100 transition-opacity"
          style={{ color: "var(--ink-muted)" }}
        />
      </div>
      <h3 className="text-sm leading-snug mt-1" style={{ color: "var(--ink-strong)" }}>
        {issue.title}
      </h3>
      <div className="mt-2 flex items-center gap-1.5 flex-wrap">
        {issue.labels.slice(0, 3).map((l) => (
          <span key={l} className="pill-muted pill !text-[9px] !tracking-wider !py-0">
            {l}
          </span>
        ))}
        {issue.assignee && (
          <span className="text-[10px] font-mono" style={{ color: "var(--ink-muted)" }}>
            @{issue.assignee}
          </span>
        )}
      </div>
      {issue.state === "closed" && (
        <div className="mt-1.5 inline-flex items-center gap-1 text-[10px]" style={{ color: "var(--ok)" }}>
          <CheckCircle2 className="w-3 h-3" /> closed
        </div>
      )}
    </a>
  );
}
