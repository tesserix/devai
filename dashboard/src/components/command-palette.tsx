"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  Boxes,
  BrainCog,
  ChevronsLeftRight,
  Database,
  GitBranch,
  Layers,
  LineChart,
  PackageOpen,
  Plus,
  Radio,
  Search,
  Settings,
  Users,
  Wrench,
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
    }
  }, [open]);

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
    if (active >= filtered.length) setActive(0);
  }, [filtered, active]);

  if (!open) return null;

  function runActive() {
    const cmd = filtered[active];
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  }

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
            setActive((i) => Math.min(filtered.length - 1, i + 1));
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
          <Search className="w-4 h-4" style={{ color: "var(--ink-muted)" }} />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Jump to a panel or run a command…"
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
          {filtered.length === 0 ? (
            <div className="px-4 py-6 text-sm" style={{ color: "var(--ink-muted)" }}>
              No matches.
            </div>
          ) : (
            renderGroups(filtered, active, setActive, runActive)
          )}
        </div>

        <footer
          className="px-3 py-2 border-t flex items-center justify-between text-[10px] font-mono"
          style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
        >
          <span>↑↓ to move · ↵ to open</span>
          <span>{filtered.length} command{filtered.length === 1 ? "" : "s"}</span>
        </footer>
      </div>
    </>
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
