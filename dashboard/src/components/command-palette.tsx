"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  AtSign,
  Boxes,
  FolderGit2,
  FolderKanban,
  Layers,
  ListChecks,
  Loader2,
  Lock,
  PackageOpen,
  Plus,
  Radio,
  Search,
  Settings,
  Users,
  Workflow,
  Wrench,
  XCircle,
} from "lucide-react";
import { newRunHref } from "@/lib/run-entry";

/**
 * ⌘K / Ctrl+K command palette.
 *
 * The user complaint that prompted this: "I can't remember the paths".
 * Solution: type any fragment of a panel name and jump there with
 * Enter. Same surface bundles the high-frequency actions (New task,
 * Toggle dark mode) so the keyboard does everything.
 *
 * Typing `@` switches to repo mode: it lists only the repos already
 * ONBOARDED to DevAI (onboarding store, state="onboarded") and Enter
 * opens the shared run dialog with that repo pre-selected. Onboarding a *new*
 * repo is a deliberate, separate step on /Repos —
 * the palette is for jumping to what's already enrolled, not enrolling.
 *
 * Mounting: rendered once at layout level; opens on the global
 * keyboard shortcut and is dismissable with Esc or clicking the
 * backdrop. Closed state has zero DOM cost.
 */
type Command = {
  id: string;
  label: string;
  hint?: string;
  Icon: typeof Boxes;
  group: "Navigate" | "Action";
  run: () => void;
};

type RepoOption = {
  full_name: string;
  name: string;
  owner: string;
  description?: string | null;
  tags?: string[] | null;
};

export function CommandPalette({
  toggleDark,
  onNewTask,
}: {
  toggleDark: () => void;
  onNewTask: () => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  // Repo-picker mode kicks in the moment the query starts with `@`. It lists the
  // onboarded repos; Enter opens the active one in Compose. The list is small
  // and the onboarding endpoint has no server-side search, so we fetch it once
  // and filter client-side as the user types.
  const repoMode = query.startsWith("@");
  const repoQuery = repoMode ? query.slice(1).trim() : "";
  const [allRepos, setAllRepos] = useState<RepoOption[]>([]);
  const [reposLoaded, setReposLoaded] = useState(false);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);

  // Global hotkey: ⌘K / Ctrl+K toggles; Esc closes.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((v) => !v);
        return;
      }
      if (e.key === "Escape" && open) {
        e.preventDefault();
        setOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setActive(0);
      setAllRepos([]);
      setReposLoaded(false);
      setRepoError(null);
    }
  }, [open]);

  // Fetch the onboarded repos the first time the user enters repo mode.
  // NOTE: deps are [open, repoMode, reposLoaded] ONLY — never repoLoading. An
  // earlier version listed repoLoading and aborted the fetch in cleanup, so
  // setRepoLoading(true) re-ran the effect, the cleanup aborted the in-flight
  // request, the AbortError skipped setReposLoaded, and it looped forever
  // ("Loading…" that never resolves). We guard re-entry with reposLoaded and
  // use a `cancelled` flag (not AbortController) so a successful response is
  // always read to completion.
  useEffect(() => {
    if (!open || !repoMode || reposLoaded) return;
    let cancelled = false;
    setRepoLoading(true);
    setRepoError(null);
    (async () => {
      try {
        const res = await fetch("/api/scm/onboarded?state=onboarded");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as RepoOption[];
        if (cancelled) return;
        setAllRepos(Array.isArray(data) ? data : []);
        setReposLoaded(true);
      } catch (err) {
        if (cancelled) return;
        setRepoError(err instanceof Error ? err.message : "fetch failed");
        setAllRepos([]);
      } finally {
        if (!cancelled) setRepoLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, repoMode, reposLoaded]);

  const commands: Command[] = useMemo(
    () => [
      { id: "nav-fleet", label: "Fleet", hint: "Active pipeline runs", Icon: Boxes, group: "Navigate", run: () => router.push("/") },
      { id: "nav-workflows", label: "Workflows", hint: "Browse blueprints → run → observe", Icon: Workflow, group: "Navigate", run: () => router.push("/workflows") },
      { id: "nav-runs", label: "Runs", hint: "Every blueprint execution", Icon: ListChecks, group: "Navigate", run: () => router.push("/runs") },
      { id: "nav-board", label: "Board", hint: "GitHub issue Kanban", Icon: FolderKanban, group: "Navigate", run: () => router.push("/board") },
      { id: "nav-repos", label: "Repos", hint: "Onboard repos to DevAI", Icon: FolderGit2, group: "Navigate", run: () => router.push("/repos") },
      { id: "nav-blueprint", label: "Blueprints", hint: "Build + publish DAGs", Icon: Layers, group: "Navigate", run: () => router.push("/blueprint") },
      { id: "nav-agents", label: "Agents", hint: "Catalogued agents", Icon: Users, group: "Navigate", run: () => router.push("/agents") },
      { id: "nav-registry", label: "Registry", hint: "Skills / prompts / MCP / agents", Icon: PackageOpen, group: "Navigate", run: () => router.push("/registry") },
      { id: "nav-gateway", label: "Gateway", hint: "Agentgateway + LLM proxy health", Icon: Radio, group: "Navigate", run: () => router.push("/gateway") },
      { id: "nav-tools", label: "Tools", hint: "Per-role MCP allow-lists", Icon: Wrench, group: "Navigate", run: () => router.push("/tools") },
      { id: "nav-settings", label: "Settings", Icon: Settings, group: "Navigate", run: () => router.push("/settings") },
      { id: "action-new", label: "New task", hint: "Dispatch a pipeline run", Icon: Plus, group: "Action", run: onNewTask },
      { id: "action-dark", label: "Toggle theme", hint: "Light / dark", Icon: Settings, group: "Action", run: toggleDark },
    ],
    [router, onNewTask, toggleDark]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) =>
        c.label.toLowerCase().includes(q) ||
        (c.hint?.toLowerCase().includes(q) ?? false)
    );
  }, [commands, query]);

  const repos = useMemo(() => {
    const q = repoQuery.toLowerCase();
    if (!q) return allRepos;
    return allRepos.filter(
      (r) =>
        r.full_name.toLowerCase().includes(q) ||
        (r.description?.toLowerCase().includes(q) ?? false)
    );
  }, [allRepos, repoQuery]);

  // Keep the active index inside bounds when filtering changes the list.
  useEffect(() => {
    const len = repoMode ? repos.length : filtered.length;
    if (active >= len) setActive(0);
  }, [filtered, repos, repoMode, active]);

  if (!open) return null;

  function runActive() {
    if (repoMode) {
      const repo = repos[active];
      if (!repo) return;
      setOpen(false);
      router.push(newRunHref(repo.full_name));
      return;
    }
    const cmd = filtered[active];
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  }

  const listLength = repoMode ? repos.length : filtered.length;

  return (
    <>
      <div className="cmdk-backdrop" onClick={() => setOpen(false)} />
      <div
        className="cmdk-shell"
        role="dialog"
        aria-modal="true"
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((i) => Math.min(Math.max(listLength - 1, 0), i + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => Math.max(0, i - 1));
          } else if (e.key === "Enter") {
            e.preventDefault();
            runActive();
          }
        }}
      >
        <header
          className="flex items-center gap-2 px-4 py-3 border-b"
          style={{ borderColor: "var(--border-subtle)" }}
        >
          {repoMode ? (
            <AtSign className="w-4 h-4" style={{ color: "var(--accent)" }} />
          ) : (
            <Search className="w-4 h-4" style={{ color: "var(--ink-muted)" }} />
          )}
          <input
            autoFocus
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            placeholder={
              repoMode
                ? "Jump to an onboarded repo…"
                : "Jump to a panel · type @ for onboarded repos…"
            }
            className="flex-1 bg-transparent text-sm outline-none"
            style={{ color: "var(--ink)" }}
          />
          <kbd
            className="font-mono text-[10px] px-1.5 py-0.5 rounded border"
            style={{
              color: "var(--ink-muted)",
              borderColor: "var(--border-subtle)",
              background: "var(--surface-muted)",
            }}
          >
            ESC
          </kbd>
        </header>

        <div className="max-h-[55vh] overflow-y-auto py-1">
          {repoMode
            ? renderRepoList(repos, active, setActive, runActive, repoLoading, repoError, repoQuery)
            : filtered.length === 0
              ? (
                <div className="px-4 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
                  No matches.
                </div>
              )
              : renderGroups(filtered, active, setActive, runActive)}
        </div>

        <footer
          className="px-3 py-2 border-t flex items-center justify-between text-[10px] font-mono"
          style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
        >
          <span>
            {repoMode ? "↑↓ to move · ↵ to open in Compose" : "↑↓ to move · ↵ to open · @ for repos"}
          </span>
          <span>
            {repoMode
              ? `${repos.length} onboarded repo${repos.length === 1 ? "" : "s"}`
              : `${filtered.length} command${filtered.length === 1 ? "" : "s"}`}
          </span>
        </footer>
      </div>
    </>
  );
}

function renderRepoList(
  repos: RepoOption[],
  active: number,
  setActive: (n: number) => void,
  runActive: () => void,
  loading: boolean,
  error: string | null,
  q: string
) {
  if (error) {
    return (
      <div className="px-4 py-6 text-sm flex items-start gap-2" style={{ color: "var(--ink-muted)" }}>
        <XCircle className="w-4 h-4 mt-0.5" style={{ color: "var(--accent)" }} />
        <span>Failed to load repos — {error}</span>
      </div>
    );
  }
  if (loading && repos.length === 0) {
    return (
      <div className="px-4 py-6 text-sm flex items-center gap-2" style={{ color: "var(--ink-muted)" }}>
        <Loader2 className="w-4 h-4 animate-spin" />
        <span>Loading onboarded repos…</span>
      </div>
    );
  }
  if (repos.length === 0) {
    return (
      <div className="px-4 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
        {q ? (
          `No onboarded repo matches "${q}".`
        ) : (
          <>
            No repos onboarded to DevAI yet.{" "}
            <a href="/repos" className="font-medium hover:underline" style={{ color: "var(--accent)" }}>
              Onboard one in Repos →
            </a>
          </>
        )}
      </div>
    );
  }
  return (
    <section>
      <div className="px-4 pt-2 pb-1 label-eyebrow">Onboarded repos</div>
      <ul>
        {repos.map((r, idx) => {
          const isActive = idx === active;
          return (
            <li key={r.full_name}>
              <button
                type="button"
                onMouseEnter={() => setActive(idx)}
                onClick={() => runActive()}
                className="w-full flex items-center gap-3 px-4 py-2 text-sm text-left transition-colors"
                style={{
                  background: isActive ? "var(--accent-soft-bg-2)" : "transparent",
                  color: isActive ? "var(--accent-soft-ink)" : "var(--ink)",
                }}
              >
                <FolderGit2
                  className="w-4 h-4 shrink-0"
                  style={{ color: isActive ? "var(--accent)" : "var(--ink-muted)" }}
                />
                <span className="flex-1 truncate">
                  <span style={{ color: "var(--ink-muted)" }}>{r.owner}/</span>
                  <span>{r.name}</span>
                </span>
                {r.tags?.includes("private") && (
                  <Lock className="w-3 h-3 shrink-0" style={{ color: "var(--ink-muted)" }} />
                )}
                {isActive && (
                  <ArrowRight className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function renderGroups(
  list: Command[],
  active: number,
  setActive: (n: number) => void,
  runActive: () => void
) {
  // Stable group order; the active index is global across all groups.
  const order: Command["group"][] = ["Navigate", "Action"];
  const groups = new Map<Command["group"], Command[]>();
  for (const c of list) {
    const existing = groups.get(c.group) ?? [];
    existing.push(c);
    groups.set(c.group, existing);
  }
  let globalIdx = -1;
  return order
    .filter((g) => groups.has(g))
    .map((g) => (
      <section key={g}>
        <div className="px-4 pt-2 pb-1 label-eyebrow">{g}</div>
        <ul>
          {groups.get(g)!.map((cmd) => {
            globalIdx += 1;
            const isActive = globalIdx === active;
            const captured = globalIdx;
            return (
              <li key={cmd.id}>
                <button
                  type="button"
                  onMouseEnter={() => setActive(captured)}
                  onClick={() => runActive()}
                  className="w-full flex items-center gap-3 px-4 py-2 text-sm text-left transition-colors"
                  style={{
                    background: isActive ? "var(--accent-soft-bg-2)" : "transparent",
                    color: isActive ? "var(--accent-soft-ink)" : "var(--ink)",
                  }}
                >
                  <cmd.Icon
                    className="w-4 h-4"
                    style={{ color: isActive ? "var(--accent)" : "var(--ink-muted)" }}
                  />
                  <span className="flex-1">{cmd.label}</span>
                  {cmd.hint && (
                    <span className="text-xs truncate" style={{ color: "var(--ink-muted)" }}>
                      {cmd.hint}
                    </span>
                  )}
                  {isActive && <ArrowRight className="w-3.5 h-3.5" style={{ color: "var(--accent)" }} />}
                </button>
              </li>
            );
          })}
        </ul>
      </section>
    ));
}
