"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ExternalLink,
  FolderGit2,
  GitPullRequestArrow,
  RefreshCw,
  UserPlus,
} from "lucide-react";
import {
  api,
  type CatalogRepo,
  type OnboardingStateValue,
  type RepoCatalogPage,
} from "@/lib/api";

type Tab = "all" | "onboarded" | "available";

const PER_PAGE = 30;

function StatePill({ state }: { state: OnboardingStateValue }) {
  if (state === "onboarded")
    return <span className="pill pill-ok"><span className="dot dot-ok" />Onboarded</span>;
  if (state === "pending_pr")
    return <span className="pill pill-warn"><span className="dot dot-warn" />Pending PR</span>;
  if (state === "dormant")
    return <span className="pill pill-error"><span className="dot dot-error" />Dormant</span>;
  return <span className="pill pill-muted"><span className="dot dot-muted" />Available</span>;
}

export default function ReposPage() {
  const [data, setData] = useState<RepoCatalogPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [tab, setTab] = useState<Tab>("all");

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [assignTarget, setAssignTarget] = useState<CatalogRepo | null>(null);

  const load = useCallback(
    async (opts: { refresh?: boolean } = {}) => {
      setLoading(true);
      setError(null);
      try {
        const res = await api.listOrgRepos({
          q: search,
          page,
          per_page: PER_PAGE,
          refresh: opts.refresh,
        });
        setData(res);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [search, page]
  );

  useEffect(() => {
    void load();
  }, [load]);

  // Debounce the search box into the query the API sees.
  useEffect(() => {
    const t = setTimeout(() => {
      setPage(1);
      setSearch(query.trim());
    }, 250);
    return () => clearTimeout(t);
  }, [query]);

  const visible = useMemo(() => {
    const repos = data?.repos ?? [];
    if (tab === "onboarded") return repos.filter((r) => r.state === "onboarded");
    if (tab === "available") return repos.filter((r) => r.state !== "onboarded");
    return repos;
  }, [data, tab]);

  const selectable = visible.filter((r) => r.state === "discovered");
  const allSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.full_name));

  function toggle(full: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(full)) next.delete(full);
      else next.add(full);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) selectable.forEach((r) => next.delete(r.full_name));
      else selectable.forEach((r) => next.add(r.full_name));
      return next;
    });
  }

  async function onboardSelected() {
    if (selected.size === 0) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const repos = [...selected].map((full) => {
        const [owner, name] = full.split("/");
        return { owner, name };
      });
      const res = await api.onboardRepos(repos);
      const ok = res.succeeded.length;
      const failed = res.failed.length;
      setNotice(
        `Onboarding started: ${ok} PR${ok === 1 ? "" : "s"} opened${
          failed ? `, ${failed} failed (${res.failed.map((f) => f.repo).join(", ")})` : ""
        }.`
      );
      setSelected(new Set());
      await load({ refresh: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function markReady(repo: CatalogRepo) {
    setBusy(true);
    setError(null);
    try {
      await api.markOnboardingPRReady(repo.owner, repo.name);
      setNotice(`PR #${repo.pr_number} for ${repo.full_name} is now ready for review.`);
      await load({ refresh: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function mergePR(repo: CatalogRepo) {
    setBusy(true);
    setError(null);
    try {
      await api.mergeOnboardingPR(repo.owner, repo.name);
      setNotice(`Merged onboarding PR for ${repo.full_name} — now onboarded.`);
      await load({ refresh: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const onboardedCount = data?.onboarded_count ?? 0;
  const totalPages = data?.total_pages ?? 1;

  return (
    <div className="p-7 space-y-5">
      {/* Header */}
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="label-eyebrow">Platform</div>
          <h1
            className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
            style={{ color: "var(--ink-strong)" }}
          >
            <FolderGit2 className="w-5 h-5" style={{ color: "var(--accent)" }} /> Repos
          </h1>
          <p className="text-sm mt-1 flex items-center gap-2" style={{ color: "var(--ink-soft)" }}>
            <span className="font-mono" style={{ color: "var(--ink)" }}>{onboardedCount}</span>
            onboarded · page {data?.page ?? page} of {totalPages}
            {data && (
              <span className="pill pill-muted" title="Served from the 5-min catalog cache">
                {data.cache_hit ? "Cached" : "Live"}
              </span>
            )}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search repos…"
            className="field !py-1.5 w-56"
          />
          <button
            type="button"
            className="btn-ghost !py-1.5"
            title="Refresh from GitHub (bypass cache)"
            onClick={() => load({ refresh: true })}
            disabled={loading || busy}
          >
            <RefreshCw className="w-4 h-4" />
          </button>
          <button
            type="button"
            className="btn-primary !py-1.5"
            onClick={onboardSelected}
            disabled={busy || selected.size === 0}
          >
            Onboard{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>
        </div>
      </header>

      {/* Tabs */}
      <div className="flex items-center gap-1">
        {(["all", "onboarded", "available"] as Tab[]).map((t) => {
          const active = tab === t;
          return (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className="px-3 py-1 rounded-md text-xs font-mono uppercase tracking-wider transition-colors"
              style={{
                background: active ? "var(--surface-hover)" : "transparent",
                color: active ? "var(--ink-strong)" : "var(--ink-muted)",
                border: `1px solid ${active ? "var(--border-strong)" : "transparent"}`,
              }}
            >
              {t}
            </button>
          );
        })}
      </div>

      {notice && (
        <div
          className="rounded-md px-3 py-2 text-sm"
          style={{
            background: "var(--ok-soft-bg)",
            color: "var(--ok-ink)",
            border: "1px solid var(--ok-soft-bd)",
          }}
        >
          {notice}
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

      {/* Table */}
      <div className="panel overflow-hidden p-0">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 36 }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleAll}
                  aria-label="Select all"
                  disabled={selectable.length === 0}
                />
              </th>
              <th>Repository</th>
              <th>Language</th>
              <th>Status</th>
              <th style={{ textAlign: "right" }}>Action</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5} style={{ color: "var(--ink-muted)" }}>Loading…</td>
              </tr>
            ) : visible.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ color: "var(--ink-muted)" }}>
                  {data && data.total === 0
                    ? "No repositories visible to the GitHub App in this org."
                    : `No repos in this ${tab} view.`}
                </td>
              </tr>
            ) : (
              visible.map((r) => {
                const canSelect = r.state === "discovered";
                return (
                  <tr key={r.full_name}>
                    <td>
                      <input
                        type="checkbox"
                        checked={selected.has(r.full_name)}
                        onChange={() => toggle(r.full_name)}
                        disabled={!canSelect}
                        aria-label={`Select ${r.full_name}`}
                      />
                    </td>
                    <td>
                      <a
                        href={r.html_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 font-mono"
                        style={{ color: "var(--ink-strong)" }}
                      >
                        {r.full_name}
                        <ExternalLink className="w-3 h-3" style={{ color: "var(--ink-muted)" }} />
                      </a>
                      {r.description && (
                        <div className="text-xs mt-0.5" style={{ color: "var(--ink-muted)" }}>
                          {r.description}
                        </div>
                      )}
                    </td>
                    <td style={{ color: "var(--ink-soft)" }}>{r.language || "—"}</td>
                    <td><StatePill state={r.state} /></td>
                    <td style={{ textAlign: "right" }}>
                      {r.state === "pending_pr" ? (
                        <div className="inline-flex items-center gap-1.5">
                          {r.pr_url && (
                            <a href={r.pr_url} target="_blank" rel="noreferrer" className="btn-ghost !py-1 !px-2 !text-xs">
                              <GitPullRequestArrow className="w-3.5 h-3.5" />
                              {r.draft ? "Draft" : "PR"} #{r.pr_number}
                            </a>
                          )}
                          <button
                            type="button"
                            className="btn-ghost !py-1 !px-2 !text-xs"
                            onClick={() => setAssignTarget(r)}
                            disabled={busy}
                            title="Request a reviewer"
                          >
                            <UserPlus className="w-3.5 h-3.5" /> Assign
                          </button>
                          {r.draft && (
                            <button
                              type="button"
                              className="btn-ghost !py-1 !px-2 !text-xs"
                              onClick={() => markReady(r)}
                              disabled={busy}
                              title="Promote the draft PR to ready for review"
                            >
                              Mark ready
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn-secondary !py-1 !px-2 !text-xs"
                            onClick={() => mergePR(r)}
                            disabled={busy}
                            title={r.draft ? "Marks ready, then merges" : "Merge the onboarding PR"}
                          >
                            Merge
                          </button>
                        </div>
                      ) : r.state === "onboarded" ? (
                        <span className="text-xs" style={{ color: "var(--ink-muted)" }}>✓ enrolled</span>
                      ) : (
                        <span className="text-xs" style={{ color: "var(--ink-muted)" }}>—</span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-center gap-2">
        <button
          type="button"
          className="btn-secondary !py-1 !px-3 !text-xs"
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={!data?.has_prev || loading}
        >
          ‹ Prev
        </button>
        <span className="text-xs font-mono" style={{ color: "var(--ink-muted)" }}>
          Page {data?.page ?? page}
        </span>
        <button
          type="button"
          className="btn-secondary !py-1 !px-3 !text-xs"
          onClick={() => setPage((p) => p + 1)}
          disabled={!data?.has_next || loading}
        >
          Next ›
        </button>
      </div>

      {assignTarget && (
        <AssignReviewerModal
          repo={assignTarget}
          onClose={() => setAssignTarget(null)}
          onAssigned={(msg) => {
            setNotice(msg);
            setAssignTarget(null);
          }}
        />
      )}
    </div>
  );
}

function AssignReviewerModal({
  repo,
  onClose,
  onAssigned,
}: {
  repo: CatalogRepo;
  onClose: () => void;
  onAssigned: (msg: string) => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    const reviewers = value
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (reviewers.length === 0) return;
    setBusy(true);
    setErr(null);
    try {
      await api.assignReviewer(repo.owner, repo.name, reviewers);
      onAssigned(`Requested review on ${repo.full_name} PR #${repo.pr_number} from ${reviewers.join(", ")}.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "var(--surface-overlay)" }}
      onClick={onClose}
    >
      <div className="panel-raised p-5 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <h2 className="font-serif text-lg" style={{ color: "var(--ink-strong)" }}>
          Assign reviewer
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
          Request review on the onboarding PR for{" "}
          <span className="font-mono" style={{ color: "var(--ink)" }}>{repo.full_name}</span>. They
          approve, then you merge.
        </p>
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="github logins or org/team (comma-separated)"
          className="field mt-3 w-full"
        />
        {err && (
          <div className="text-xs font-mono mt-2" style={{ color: "var(--error-ink)" }}>{err}</div>
        )}
        <div className="flex justify-end gap-2 mt-4">
          <button type="button" className="btn-ghost !py-1.5" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="btn-primary !py-1.5" onClick={submit} disabled={busy || !value.trim()}>
            Request review
          </button>
        </div>
      </div>
    </div>
  );
}
