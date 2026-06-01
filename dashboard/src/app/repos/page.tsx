"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ExternalLink,
  FolderGit2,
  GitPullRequestArrow,
  Plus,
  RefreshCw,
  SearchCheck,
  UserPlus,
} from "lucide-react";
import {
  api,
  type CatalogRepo,
  type OnboardingStateValue,
  type RepoCatalogPage,
} from "@/lib/api";
import { useToast } from "@/components/toast";

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
  const toast = useToast();
  const [data, setData] = useState<RepoCatalogPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [tab, setTab] = useState<Tab>("all");

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [assignTarget, setAssignTarget] = useState<CatalogRepo | null>(null);
  const [creating, setCreating] = useState(false);

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
    // "All" view: when any repo is onboarded, float onboarded to the top (then
    // pending-PR), preserving the server's recency order within each group. So
    // a workspace with onboarded repos always opens with them first; one with
    // none just shows the plain recency list. Array.sort is stable in modern JS.
    const rank = (s: OnboardingStateValue) => (s === "onboarded" ? 0 : s === "pending_pr" ? 1 : 2);
    return [...repos].sort((a, b) => rank(a.state) - rank(b.state));
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
    const count = selected.size;
    const tid = toast.loading(`Opening onboarding PR${count === 1 ? "" : "s"} for ${count} repo${count === 1 ? "" : "s"}…`);
    try {
      const repos = [...selected].map((full) => {
        const [owner, name] = full.split("/");
        return { owner, name };
      });
      const res = await api.onboardRepos(repos);
      const ok = res.succeeded.length;
      const failed = res.failed.length;
      if (failed) {
        toast.update(
          tid,
          ok ? "info" : "error",
          `Onboarding: ${ok} PR${ok === 1 ? "" : "s"} opened, ${failed} failed (${res.failed.map((f) => f.repo).join(", ")}).`
        );
      } else {
        toast.update(tid, "success", `Onboarding started: ${ok} PR${ok === 1 ? "" : "s"} opened.`);
      }
      setSelected(new Set());
      await load({ refresh: true });
    } catch (e) {
      toast.update(tid, "error", `Onboard failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  // Re-scan: server-side reconcile against the `.platform/devai.yaml` marker
  // (the source of truth). Adopts repos whose marker was added out-of-band,
  // and drops repos whose marker (or whole repo) was deleted — so the list
  // matches reality on demand without waiting for the hourly job.
  async function rescan() {
    setBusy(true);
    const tid = toast.loading("Re-scanning org for .platform/devai.yaml markers…");
    try {
      const r = await api.reconcileRepos();
      const errs = r.errors?.length ? `, ${r.errors.length} error(s)` : "";
      toast.update(
        tid,
        "success",
        `Re-scan complete — ${r.scanned} scanned, ${r.reconciled} updated${errs}.`
      );
      await load({ refresh: true });
    } catch (e) {
      toast.update(tid, "error", `Re-scan failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function markReady(repo: CatalogRepo) {
    setBusy(true);
    const tid = toast.loading(`Marking PR #${repo.pr_number} ready (${repo.full_name})…`);
    try {
      await api.markOnboardingPRReady(repo.owner, repo.name);
      toast.update(tid, "success", `PR #${repo.pr_number} for ${repo.full_name} is ready for review.`);
      await load({ refresh: true });
    } catch (e) {
      toast.update(tid, "error", `Mark ready failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function mergePR(repo: CatalogRepo) {
    setBusy(true);
    const tid = toast.loading(`Merging onboarding PR for ${repo.full_name}…`);
    try {
      await api.mergeOnboardingPR(repo.owner, repo.name);
      toast.update(tid, "success", `Merged onboarding PR for ${repo.full_name} — now onboarded.`);
      await load({ refresh: true });
    } catch (e) {
      toast.update(tid, "error", `Merge failed: ${e instanceof Error ? e.message : String(e)}`);
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
            className="btn-ghost !py-1.5 flex items-center gap-1.5"
            title="Re-scan the org for .platform/devai.yaml markers — adopt newly-marked repos and drop ones whose marker (or repo) was deleted"
            onClick={rescan}
            disabled={loading || busy}
          >
            <SearchCheck className="w-4 h-4" /> Re-scan
          </button>
          <button
            type="button"
            className="btn-ghost !py-1.5"
            title="Create a brand-new repo, scaffold defaults + CI/PR/release gates, and auto-onboard it"
            onClick={() => setCreating(true)}
            disabled={busy}
          >
            <Plus className="w-4 h-4" /> New repo
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
          onAssigned={() => setAssignTarget(null)}
        />
      )}

      {creating && (
        <NewRepoModal
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            void load({ refresh: true });
          }}
        />
      )}
    </div>
  );
}

function NewRepoModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [techStack, setTechStack] = useState("");
  const [isPrivate, setIsPrivate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const validName = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(name.trim());

  async function submit() {
    if (busy) return;
    if (!validName) {
      setErr("Enter a valid repo name (letters, digits, '.', '-', '_').");
      return;
    }
    setBusy(true);
    setErr(null);
    const repoName = name.trim();
    const tid = toast.loading(`Creating ${repoName}…`);
    try {
      const res = await api.createAndOnboardRepo({
        name: repoName,
        description: description.trim(),
        private: isPrivate,
        tech_stack: techStack.trim(),
      });
      const skipped = res.files_skipped?.length ?? 0;
      toast.update(
        tid,
        "success",
        `Created ${res.repo} — ${res.files_created.length} files scaffolded and onboarded.` +
          (res.branch_protected ? " Branch protection on." : "") +
          (skipped ? ` ${skipped} workflow file(s) skipped — token needs the 'Workflows' permission.` : "") +
          (res.adopted ? " (adopted an existing repo)" : "")
      );
      onCreated();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg);
      toast.update(tid, "error", `Create failed: ${msg}`);
    } finally {
      setBusy(false);
    }
  }

  // While a create is in flight the whole form is locked.
  function close() {
    if (!busy) onClose();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "var(--surface-overlay)" }}
      onClick={close}
    >
      <div className="panel-raised p-5 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <h2 className="font-serif text-lg" style={{ color: "var(--ink-strong)" }}>
          New repository
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
          Creates the repo, scaffolds <span className="font-mono">README</span>,{" "}
          <span className="font-mono">.github/workflows</span> (CI / PR / release gates),
          Dependabot &amp; CODEOWNERS, then drops the{" "}
          <span className="font-mono">.platform/devai.yaml</span> marker so it onboards
          automatically.
        </p>

        <label className="block text-xs mt-4 mb-1" style={{ color: "var(--ink-muted)" }}>
          Name
        </label>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="my-service"
          className="field w-full"
          disabled={busy}
        />

        <label className="block text-xs mt-3 mb-1" style={{ color: "var(--ink-muted)" }}>
          Description <span style={{ opacity: 0.6 }}>(optional)</span>
        </label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="One-line summary"
          className="field w-full"
          disabled={busy}
        />

        <label className="block text-xs mt-3 mb-1" style={{ color: "var(--ink-muted)" }}>
          Tech stack hint <span style={{ opacity: 0.6 }}>(optional)</span>
        </label>
        <input
          value={techStack}
          onChange={(e) => setTechStack(e.target.value)}
          placeholder="e.g. Go service, Next.js app…"
          className="field w-full"
          disabled={busy}
        />

        <label
          className="flex items-center gap-2 mt-3 text-sm"
          style={{ color: "var(--ink-soft)", opacity: busy ? 0.6 : 1 }}
        >
          <input
            type="checkbox"
            checked={isPrivate}
            onChange={(e) => setIsPrivate(e.target.checked)}
            disabled={busy}
          />
          Private repository
        </label>

        {err && (
          <div className="text-xs font-mono mt-3" style={{ color: "var(--error-ink)" }}>{err}</div>
        )}
        {busy && (
          <div className="text-xs mt-3" style={{ color: "var(--ink-muted)" }}>
            Creating &amp; onboarding… this can take a few seconds. The form is locked until it finishes.
          </div>
        )}
        <div className="flex justify-end gap-2 mt-4">
          <button type="button" className="btn-ghost !py-1.5" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary !py-1.5"
            onClick={submit}
            disabled={busy || !name.trim()}
          >
            {busy ? "Creating…" : "Create & onboard"}
          </button>
        </div>
      </div>
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
  onAssigned: () => void;
}) {
  const toast = useToast();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function close() {
    if (!busy) onClose();
  }

  async function submit() {
    if (busy) return;
    const reviewers = value
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (reviewers.length === 0) return;
    setBusy(true);
    setErr(null);
    const tid = toast.loading(`Requesting review on ${repo.full_name} PR #${repo.pr_number}…`);
    try {
      await api.assignReviewer(repo.owner, repo.name, reviewers);
      toast.update(
        tid,
        "success",
        `Requested review on ${repo.full_name} PR #${repo.pr_number} from ${reviewers.join(", ")}.`
      );
      onAssigned();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg);
      toast.update(tid, "error", `Assign reviewer failed: ${msg}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "var(--surface-overlay)" }}
      onClick={close}
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
          disabled={busy}
        />
        {err && (
          <div className="text-xs font-mono mt-2" style={{ color: "var(--error-ink)" }}>{err}</div>
        )}
        <div className="flex justify-end gap-2 mt-4">
          <button type="button" className="btn-ghost !py-1.5" onClick={close} disabled={busy}>
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
