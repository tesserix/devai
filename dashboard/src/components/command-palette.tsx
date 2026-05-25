"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  AtSign,
  Boxes,
  BrainCog,
  CheckCircle2,
  ChevronsLeftRight,
  Database,
  GitBranch,
  GitMerge,
  Layers,
  LineChart,
  Loader2,
  PackageOpen,
  Plus,
  Radio,
  Search,
  Settings,
  ShieldCheck,
  Users,
  Wrench,
  XCircle,
} from "lucide-react";

/**
 * ⌘K / Ctrl+K command palette.
 *
 * The user complaint that prompted this: "I can't remember the paths".
 * Solution: type any fragment of a panel name and jump there with
 * Enter. Same surface bundles the high-frequency actions (New task,
 * Toggle dark mode) so the keyboard does everything.
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
  default_branch?: string | null;
  language?: string | null;
  private?: boolean;
};

type InitState =
  | { phase: "idle" }
  | { phase: "confirm"; repo: RepoOption }
  | { phase: "running"; repo: RepoOption; step: "init" | "scan" }
  | { phase: "done"; repo: RepoOption; prUrl?: string | null; alreadyInitialised?: boolean }
  | { phase: "error"; repo: RepoOption; message: string };

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

  // Repo-picker mode kicks in the moment the query starts with `@`.
  // Triggers a debounced fetch of repos that are NOT yet enrolled
  // (no .platform/devai.yaml). Selecting one opens an init flow.
  const repoMode = query.startsWith("@");
  const repoQuery = repoMode ? query.slice(1).trim() : "";
  const [repos, setRepos] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [init, setInit] = useState<InitState>({ phase: "idle" });

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
      setRepos([]);
      setRepoError(null);
      setInit({ phase: "idle" });
    }
  }, [open]);

  // Debounced fetch for repo-picker mode. We hit the SCM router with
  // initialised=false so the backend only returns repos that don't
  // already have .platform/devai.yaml on their default branch.
  useEffect(() => {
    if (!open || !repoMode) {
      setRepos([]);
      setRepoError(null);
      return;
    }
    const controller = new AbortController();
    setRepoLoading(true);
    setRepoError(null);
    const handle = window.setTimeout(async () => {
      try {
        const url = `/api/scm/repos?initialised=false${repoQuery ? `&q=${encodeURIComponent(repoQuery)}` : ""}`;
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as RepoOption[];
        setRepos(Array.isArray(data) ? data : []);
      } catch (err) {
        if ((err as { name?: string }).name === "AbortError") return;
        setRepoError(err instanceof Error ? err.message : "fetch failed");
        setRepos([]);
      } finally {
        setRepoLoading(false);
      }
    }, 180);
    return () => {
      controller.abort();
      window.clearTimeout(handle);
    };
  }, [open, repoMode, repoQuery]);

  const commands: Command[] = useMemo(
    () => [
      { id: "nav-fleet", label: "Fleet", hint: "Active pipeline runs", Icon: Boxes, group: "Navigate", run: () => router.push("/") },
      { id: "nav-workflows", label: "Workflows", hint: "Cross-functional gates", Icon: GitBranch, group: "Navigate", run: () => router.push("/workflows") },
      { id: "nav-blueprint", label: "Blueprint", hint: "DAG of stages", Icon: Layers, group: "Navigate", run: () => router.push("/blueprint") },
      { id: "nav-agents", label: "Agents", hint: "Catalogued agents", Icon: Users, group: "Navigate", run: () => router.push("/agents") },
      { id: "nav-memory", label: "Memory", hint: "Episodic + semantic", Icon: BrainCog, group: "Navigate", run: () => router.push("/memory") },
      { id: "nav-registry", label: "Registry", hint: "Skills / prompts / MCP / agents", Icon: PackageOpen, group: "Navigate", run: () => router.push("/registry") },
      { id: "nav-gateway", label: "Gateway", hint: "Agentgateway + LLM proxy health", Icon: Radio, group: "Navigate", run: () => router.push("/gateway") },
      { id: "nav-catalog", label: "Catalog", hint: "Resolved capability map", Icon: Database, group: "Navigate", run: () => router.push("/catalog") },
      { id: "nav-control", label: "Control", hint: "Manual pause / takeover", Icon: ChevronsLeftRight, group: "Navigate", run: () => router.push("/control") },
      { id: "nav-analytics", label: "Analytics", hint: "Cost + duration trends", Icon: LineChart, group: "Navigate", run: () => router.push("/analytics") },
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
      setInit({ phase: "confirm", repo });
      return;
    }
    const cmd = filtered[active];
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  }

  async function runInit(repo: RepoOption) {
    setInit({ phase: "running", repo, step: "init" });
    try {
      const initRes = await fetch(
        `/api/scm/repos/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/initialise`,
        { method: "POST" }
      );
      if (!initRes.ok) {
        const detail = await initRes.text();
        throw new Error(detail || `init HTTP ${initRes.status}`);
      }
      const initBody = (await initRes.json()) as {
        already_initialised?: boolean;
        pull_request_url?: string | null;
      };

      // Fire scan even when already initialised so the profile is fresh.
      setInit({ phase: "running", repo, step: "scan" });
      try {
        await fetch(
          `/api/scm/repos/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/scan`,
          { method: "POST" }
        );
      } catch {
        /* scan is best-effort — init success still matters */
      }

      setInit({
        phase: "done",
        repo,
        prUrl: initBody.pull_request_url ?? null,
        alreadyInitialised: initBody.already_initialised ?? false,
      });
    } catch (err) {
      setInit({
        phase: "error",
        repo,
        message: err instanceof Error ? err.message : "init failed",
      });
    }
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
          if (init.phase !== "idle" && init.phase !== "confirm") return;
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
                ? "Find an un-enrolled repo to initialise…"
                : "Jump to a panel · type @ to enrol a repo…"
            }
            className="flex-1 bg-transparent text-sm outline-none"
            style={{ color: "var(--ink)" }}
            disabled={init.phase !== "idle" && init.phase !== "confirm"}
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
          {init.phase === "confirm" && renderConfirm(init.repo, () => runInit(init.repo), () => setInit({ phase: "idle" }))}
          {init.phase === "running" && renderRunning(init.repo, init.step)}
          {init.phase === "done" && renderDone(init.repo, init.prUrl, init.alreadyInitialised, () => { setOpen(false); router.push("/workflows"); })}
          {init.phase === "error" && renderError(init.repo, init.message, () => setInit({ phase: "confirm", repo: init.repo }))}
          {init.phase === "idle" &&
            (repoMode
              ? renderRepoList(repos, active, setActive, runActive, repoLoading, repoError, repoQuery)
              : filtered.length === 0
                ? (
                  <div className="px-4 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
                    No matches.
                  </div>
                )
                : renderGroups(filtered, active, setActive, runActive))}
        </div>

        <footer
          className="px-3 py-2 border-t flex items-center justify-between text-[10px] font-mono"
          style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
        >
          <span>
            {repoMode ? "↑↓ to move · ↵ to initialise" : "↑↓ to move · ↵ to open · @ for repos"}
          </span>
          <span>
            {repoMode
              ? `${repos.length} repo${repos.length === 1 ? "" : "s"}`
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
        <span>Searching un-enrolled repos…</span>
      </div>
    );
  }
  if (repos.length === 0) {
    return (
      <div className="px-4 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
        {q
          ? `No un-enrolled repo matches "${q}".`
          : "All accessible repos are already enrolled. Type to narrow the search."}
      </div>
    );
  }
  return (
    <section>
      <div className="px-4 pt-2 pb-1 label-eyebrow">Un-enrolled repos</div>
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
                <GitBranch
                  className="w-4 h-4"
                  style={{ color: isActive ? "var(--accent)" : "var(--ink-muted)" }}
                />
                <span className="flex-1 truncate">
                  <span style={{ color: "var(--ink-muted)" }}>{r.owner}/</span>
                  <span>{r.name}</span>
                </span>
                {r.language && (
                  <span
                    className="text-[10px] px-1.5 py-0.5 rounded border font-mono"
                    style={{
                      color: "var(--ink-muted)",
                      borderColor: "var(--border-subtle)",
                      background: "var(--surface-muted)",
                    }}
                  >
                    {r.language}
                  </span>
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

function renderConfirm(repo: RepoOption, onConfirm: () => void, onCancel: () => void) {
  return (
    <div className="px-5 py-5 flex flex-col gap-4">
      <div className="flex items-center gap-2 text-sm">
        <GitMerge className="w-4 h-4" style={{ color: "var(--accent)" }} />
        <span style={{ color: "var(--ink)" }}>
          Initialise <strong>{repo.full_name}</strong> for DevAI?
        </span>
      </div>
      <p className="text-xs leading-relaxed" style={{ color: "var(--ink-muted)" }}>
        We&apos;ll branch from <code>{repo.default_branch ?? "main"}</code>, drop a{" "}
        <code>.platform/devai.yaml</code> with the default 5-lane workflow, and open a pull request.
        Then an agent will scan the repo so DevAI knows the tech stack before any run starts.
      </p>
      <div className="flex items-center gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 text-xs rounded border"
          style={{
            color: "var(--ink-muted)",
            borderColor: "var(--border-subtle)",
            background: "var(--surface)",
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          autoFocus
          className="px-3 py-1.5 text-xs rounded font-medium"
          style={{
            color: "var(--surface)",
            background: "var(--accent)",
          }}
        >
          Open enrolment PR
        </button>
      </div>
    </div>
  );
}

function renderRunning(repo: RepoOption, step: "init" | "scan") {
  return (
    <div className="px-5 py-6 flex flex-col gap-2">
      <div className="flex items-center gap-2 text-sm" style={{ color: "var(--ink)" }}>
        <Loader2 className="w-4 h-4 animate-spin" style={{ color: "var(--accent)" }} />
        <span>
          {step === "init"
            ? `Creating devai/init-platform on ${repo.full_name}…`
            : `Scanning ${repo.full_name} for stack + structure…`}
        </span>
      </div>
      <p className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
        Keep this open — both steps usually finish in a few seconds.
      </p>
    </div>
  );
}

function renderDone(
  repo: RepoOption,
  prUrl: string | null | undefined,
  alreadyInitialised: boolean | undefined,
  onClose: () => void
) {
  return (
    <div className="px-5 py-5 flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm" style={{ color: "var(--ink)" }}>
        <CheckCircle2 className="w-4 h-4" style={{ color: "var(--accent)" }} />
        <span>
          {alreadyInitialised
            ? `${repo.full_name} is already enrolled — profile refreshed.`
            : `Enrolment PR opened for ${repo.full_name}.`}
        </span>
      </div>
      <div className="flex items-center gap-2 text-[11px]" style={{ color: "var(--ink-muted)" }}>
        <ShieldCheck className="w-3.5 h-3.5" />
        <span>Repo profile cached in memory for future pipeline runs.</span>
      </div>
      <div className="flex items-center gap-2 justify-end">
        {prUrl && (
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="px-3 py-1.5 text-xs rounded border"
            style={{
              color: "var(--ink)",
              borderColor: "var(--border-subtle)",
              background: "var(--surface)",
            }}
          >
            View PR
          </a>
        )}
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-xs rounded font-medium"
          style={{ color: "var(--surface)", background: "var(--accent)" }}
        >
          Open Workflows
        </button>
      </div>
    </div>
  );
}

function renderError(repo: RepoOption, message: string, onRetry: () => void) {
  return (
    <div className="px-5 py-5 flex flex-col gap-3">
      <div className="flex items-start gap-2 text-sm" style={{ color: "var(--ink)" }}>
        <XCircle className="w-4 h-4 mt-0.5" style={{ color: "var(--accent)" }} />
        <span>Couldn&apos;t initialise {repo.full_name}.</span>
      </div>
      <p className="text-[11px] font-mono break-all" style={{ color: "var(--ink-muted)" }}>
        {message}
      </p>
      <div className="flex items-center gap-2 justify-end">
        <button
          type="button"
          onClick={onRetry}
          className="px-3 py-1.5 text-xs rounded font-medium"
          style={{ color: "var(--surface)", background: "var(--accent)" }}
        >
          Try again
        </button>
      </div>
    </div>
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
