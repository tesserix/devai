"use client";

/**
 * REPO tab on the run detail page.
 *
 * Left column: a navigable file tree backed by /api/runs/{id}/repo/tree.
 *              Clicking a folder expands inline; clicking a file loads it
 *              into the viewer on the right.
 *
 * Right column: a read-only viewer. Read-only by design — the panel
 *               disables editing controls entirely, so the *only* way to
 *               change a file is via the SCM PR. That matches the user's
 *               brief: "view and read, can edit from the REPO".
 *
 * Live updates: subscribes to /api/runs/{id}/repo/events (SSE) and
 *               flashes the affected path in the tree, then refreshes the
 *               viewer if the open file changed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";

interface TreeEntry {
  name: string;
  type: "file" | "dir";
  size: number;
  path: string;
}

interface FileBlob {
  path: string;
  encoding: "utf-8" | "base64";
  content: string;
  size: number;
  truncated: boolean;
}

interface RepoEvent {
  kind: "created" | "modified" | "deleted";
  path: string;
  agent: string;
  triggered_by: string;
  timestamp: number;
}

export function RepoPanel({ runId }: { runId: string }) {
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [rootEntries, setRootEntries] = useState<TreeEntry[]>([]);
  const [expanded, setExpanded] = useState<Record<string, TreeEntry[]>>({});
  const [openPath, setOpenPath] = useState<string>("");
  const [openBlob, setOpenBlob] = useState<FileBlob | null>(null);
  const [loading, setLoading] = useState(false);
  // Recently-changed paths (flashed in the tree). Cleared after 4s.
  const [flash, setFlash] = useState<Record<string, number>>({});
  const [streamStatus, setStreamStatus] = useState<"connecting" | "open" | "closed">("connecting");

  // ── Tree loading ───────────────────────────────────────────────────
  // refreshTree re-resolves the branch server-side (the backend follows
  // the agent's LIVE working branch) and reloads the root + every
  // expanded folder, so commits appear as the agents push them.
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const branchRef = useRef(branch);
  branchRef.current = branch;

  const refreshTree = useCallback(
    async (initial = false) => {
      if (!runId) return;
      if (initial) setLoading(true);
      try {
        const data = await api.getRepoTree(runId, "");
        setRepo(data.repo);
        setRootEntries(data.entries);
        const branchChanged = data.branch !== branchRef.current;
        setBranch(data.branch);
        if (branchChanged) {
          // New working branch (e.g. the developer just created its
          // story branch) — old expansions point at stale paths.
          setExpanded({});
        } else {
          const open = Object.keys(expandedRef.current);
          const refreshed = await Promise.all(
            open.map(async (p) => {
              try {
                const d = await api.getRepoTree(runId, p);
                return [p, d.entries] as const;
              } catch {
                return null;
              }
            }),
          );
          setExpanded(Object.fromEntries(refreshed.filter(Boolean) as [string, TreeEntry[]][]));
        }
      } catch {
        if (initial) setRootEntries([]);
      } finally {
        if (initial) setLoading(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    void refreshTree(true);
  }, [refreshTree]);

  // Fallback poll: the working branch can change without a file event
  // (branch created before the first commit lands).
  useEffect(() => {
    if (!runId) return;
    const id = window.setInterval(() => void refreshTree(), 30000);
    return () => window.clearInterval(id);
  }, [runId, refreshTree]);

  // ── SSE: live repo events ──────────────────────────────────────────
  useEffect(() => {
    if (!runId) return;
    const url = `/api/runs/${encodeURIComponent(runId)}/repo/events?replay=20`;
    const es = new EventSource(url, { withCredentials: true });
    setStreamStatus("connecting");
    es.addEventListener("hello", () => setStreamStatus("open"));
    let refreshTimer: number | undefined;
    es.addEventListener("repo", (e) => {
      try {
        const payload: RepoEvent = JSON.parse((e as MessageEvent).data);
        if (!payload.path) return;
        setFlash((prev) => ({ ...prev, [payload.path]: Date.now() }));
        // If the file currently open just changed, refresh its content.
        if (payload.path === openPath) {
          api.getRepoFile(runId, payload.path).then(setOpenBlob).catch(() => undefined);
        }
        // New/changed files must APPEAR in the tree, not just flash —
        // debounce so a burst of writes costs one reload.
        if (refreshTimer) window.clearTimeout(refreshTimer);
        refreshTimer = window.setTimeout(() => void refreshTree(), 1500);
      } catch {
        // ignore malformed
      }
    });
    es.onerror = () => setStreamStatus("closed");
    return () => {
      if (refreshTimer) window.clearTimeout(refreshTimer);
      es.close();
    };
  }, [runId, openPath, refreshTree]);

  // ── Flash decay — 4s, then drop from the highlight set ─────────────
  useEffect(() => {
    if (!Object.keys(flash).length) return;
    const id = window.setInterval(() => {
      const now = Date.now();
      setFlash((prev) => {
        const next: Record<string, number> = {};
        for (const [p, t] of Object.entries(prev)) {
          if (now - t < 4000) next[p] = t;
        }
        return next;
      });
    }, 1000);
    return () => window.clearInterval(id);
  }, [flash]);

  const toggleDir = useCallback(
    async (path: string) => {
      if (expanded[path]) {
        setExpanded((prev) => {
          const next = { ...prev };
          delete next[path];
          return next;
        });
        return;
      }
      try {
        const data = await api.getRepoTree(runId, path);
        setExpanded((prev) => ({ ...prev, [path]: data.entries }));
      } catch {
        // Folder may have been deleted between tree fetch and click.
      }
    },
    [expanded, runId],
  );

  const openFile = useCallback(
    async (path: string) => {
      setOpenPath(path);
      setOpenBlob(null);
      try {
        const blob = await api.getRepoFile(runId, path);
        setOpenBlob(blob);
      } catch {
        setOpenBlob(null);
      }
    },
    [runId],
  );

  return (
    <div className="h-full flex">
      {/* File tree */}
      <div
        className="w-72 shrink-0 overflow-y-auto"
        style={{ borderRight: "1px solid var(--border)", background: "var(--surface-muted)" }}
      >
        <div
          className="px-3 py-2 flex items-center justify-between text-[10px] font-mono uppercase tracking-wide"
          style={{ borderBottom: "1px solid var(--border-subtle)", color: "var(--ink-muted)" }}
        >
          <span className="truncate" title={repo}>
            {repo || "—"}
          </span>
          <StreamBadge status={streamStatus} />
        </div>
        {branch && (
          <div
            className="px-3 py-1 text-[10px] font-mono"
            style={{ color: "var(--ink-muted)", borderBottom: "1px solid var(--border-subtle)" }}
          >
            branch: {branch}
          </div>
        )}
        {loading ? (
          <div className="p-3 text-xs" style={{ color: "var(--ink-muted)" }}>
            loading…
          </div>
        ) : (
          <TreeList
            entries={rootEntries}
            expanded={expanded}
            flash={flash}
            depth={0}
            openPath={openPath}
            onToggle={toggleDir}
            onOpen={openFile}
          />
        )}
      </div>

      {/* Viewer */}
      <div className="flex-1 min-w-0 flex flex-col">
        <div
          className="px-4 py-2 flex items-center justify-between text-xs font-mono"
          style={{ borderBottom: "1px solid var(--border)", background: "var(--surface-muted)" }}
        >
          <span style={{ color: "var(--ink)" }}>{openPath || "select a file"}</span>
          <span style={{ color: "var(--ink-muted)" }}>read-only</span>
        </div>
        <Viewer blob={openBlob} />
      </div>
    </div>
  );
}

function StreamBadge({ status }: { status: "connecting" | "open" | "closed" }) {
  const colors = {
    connecting: { dot: "var(--ink-muted)", label: "connecting" },
    open: { dot: "var(--ok)", label: "live" },
    closed: { dot: "var(--error)", label: "offline" },
  } as const;
  const c = colors[status];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block w-1.5 h-1.5 rounded-full"
        style={{ background: c.dot }}
      />
      <span style={{ color: "var(--ink-muted)" }}>{c.label}</span>
    </span>
  );
}

function TreeList({
  entries,
  expanded,
  flash,
  depth,
  openPath,
  onToggle,
  onOpen,
}: {
  entries: TreeEntry[];
  expanded: Record<string, TreeEntry[]>;
  flash: Record<string, number>;
  depth: number;
  openPath: string;
  onToggle: (path: string) => void;
  onOpen: (path: string) => void;
}) {
  return (
    <ul className="text-sm">
      {entries.map((entry) => {
        const isFlashing = !!flash[entry.path];
        const isOpen = entry.path === openPath;
        const isDir = entry.type === "dir";
        const children = expanded[entry.path];
        return (
          <li key={entry.path}>
            <button
              onClick={() => (isDir ? onToggle(entry.path) : onOpen(entry.path))}
              className="w-full text-left flex items-center gap-1.5 px-2 py-1 text-xs transition-colors"
              style={{
                paddingLeft: `${8 + depth * 12}px`,
                background: isFlashing
                  ? "var(--accent-soft-bg)"
                  : isOpen
                    ? "var(--surface-hover)"
                    : "transparent",
                color: isOpen ? "var(--ink-strong)" : "var(--ink)",
                fontWeight: isOpen ? 500 : 400,
              }}
            >
              <span style={{ width: 12, color: "var(--ink-muted)", fontFamily: "monospace" }}>
                {isDir ? (children ? "▾" : "▸") : ""}
              </span>
              <span className="truncate">{entry.name || entry.path}</span>
              {isFlashing && (
                <span
                  className="ml-auto text-[10px] font-mono px-1 rounded"
                  style={{ background: "var(--accent)", color: "var(--primary-ink)" }}
                >
                  changed
                </span>
              )}
            </button>
            {isDir && children && (
              <TreeList
                entries={children}
                expanded={expanded}
                flash={flash}
                depth={depth + 1}
                openPath={openPath}
                onToggle={onToggle}
                onOpen={onOpen}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Viewer({ blob }: { blob: FileBlob | null }) {
  const preRef = useRef<HTMLPreElement>(null);

  const language = useMemo(() => {
    const path = blob?.path || "";
    const ext = path.split(".").pop() ?? "";
    return ext;
  }, [blob]);

  if (!blob) {
    return (
      <div
        className="flex-1 flex items-center justify-center text-sm"
        style={{ color: "var(--ink-muted)" }}
      >
        Pick a file from the tree.
      </div>
    );
  }

  if (blob.encoding === "base64") {
    return (
      <div
        className="flex-1 flex items-center justify-center text-sm"
        style={{ color: "var(--ink-muted)" }}
      >
        Binary file ({blob.size.toLocaleString()} bytes) — preview unavailable.
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto">
      {blob.truncated && (
        <div
          className="px-4 py-1 text-xs"
          style={{
            background: "var(--accent-soft-bg)",
            borderBottom: "1px solid var(--accent-soft-bd)",
            color: "var(--ink)",
          }}
        >
          File truncated at 1 MiB.
        </div>
      )}
      <pre
        ref={preRef}
        className="px-4 py-3 text-xs font-mono whitespace-pre"
        data-lang={language}
        style={{ color: "var(--ink)", lineHeight: 1.5 }}
      >
        {blob.content}
      </pre>
    </div>
  );
}
