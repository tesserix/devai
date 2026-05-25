"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { clsx } from "clsx";
import {
  Box,
  Boxes,
  BrainCog,
  ChevronsLeftRight,
  Database,
  GitBranch,
  Layers,
  LineChart,
  Moon,
  PackageOpen,
  Plus,
  Search,
  Settings,
  ShieldHalf,
  Sun,
  Users,
  Wrench,
} from "lucide-react";

/**
 * Fiber-style mission-control left nav (PLATFORM.md §14 Tactical
 * Monospace). The components here match the visual structure of the
 * reference screenshot:
 *
 *   ┌────────────────────────────┐
 *   │   ◆ DevAI                  │  logo block
 *   │   Mission control          │
 *   │                            │
 *   │   ROLE                     │
 *   │   ● Administrator    ▾     │  role chip
 *   │                            │
 *   │   ▣ Fleet              [1] │  top group (with count badges)
 *   │   ‖ Workflows              │
 *   │   ▢ Blueprint              │
 *   │   ⛨ Agents                 │
 *   │   ⌽ Memory                 │
 *   │                            │
 *   │   ▤ Registry               │  platform group
 *   │   ▦ Catalog                │
 *   │   ⇆ Control                │
 *   │   ⥤ Analytics              │
 *   │   ⚒ Tools                  │
 *   │   ⚙ Settings               │
 *   │                            │
 *   │   ☾ Dark mode              │  meta
 *   │                            │
 *   │   ┌──────────────────────┐ │
 *   │   │   + New task         │ │  primary CTA
 *   │   └──────────────────────┘ │
 *   └────────────────────────────┘
 */
type NavItem = {
  href: string;
  label: string;
  Icon: typeof Box;
  description?: string;
  badge?: string;
};

const TOP: NavItem[] = [
  { href: "/", label: "Fleet", Icon: Boxes, description: "Active pipeline runs" },
  { href: "/workflows", label: "Workflows", Icon: GitBranch, description: "Cross-functional gates" },
  { href: "/blueprint", label: "Blueprint", Icon: Layers, description: "DAG of stages" },
  { href: "/agents", label: "Agents", Icon: Users, description: "Catalogued agents" },
  { href: "/memory", label: "Memory", Icon: BrainCog, description: "Episodic + semantic" },
];

const MID: NavItem[] = [
  { href: "/registry", label: "Registry", Icon: PackageOpen, description: "Skills · Prompts · MCP · Agents" },
  { href: "/catalog", label: "Catalog", Icon: Database, description: "Resolved capability map" },
  { href: "/control", label: "Control", Icon: ChevronsLeftRight, description: "Manual pause / takeover" },
  { href: "/analytics", label: "Analytics", Icon: LineChart, description: "Cost + duration trends" },
  { href: "/tools", label: "Tools", Icon: Wrench, description: "MCP allow-lists" },
];

const BOTTOM: NavItem[] = [
  { href: "/settings", label: "Settings", Icon: Settings },
];

function isActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

export function MissionControlNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [dark, setDark] = useState(true);

  // Read + persist the theme preference. We default to dark to match
  // the Fiber screenshot; flipping persists across reloads.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem("devai-theme");
    const isDark = stored ? stored === "dark" : true;
    setDark(isDark);
    document.documentElement.classList.toggle("dark", isDark);
  }, []);

  function toggleDark() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    window.localStorage.setItem("devai-theme", next ? "dark" : "light");
  }

  function newTask() {
    // Dispatch route lives at /control — that's the action panel for
    // Pipeline.Submit. Until the dialog is ported we route there.
    router.push("/control?action=new");
  }

  return (
    <aside className="w-60 shrink-0 border-r border-[var(--surface-border)] bg-[var(--surface-1)] text-[var(--ink-100)] flex flex-col">
      {/* Logo + tag */}
      <div className="px-4 pt-4 pb-2 flex items-center gap-2.5">
        <span className="inline-flex w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-700 text-white items-center justify-center shadow-sm">
          <ShieldHalf className="w-4 h-4" />
        </span>
        <div className="min-w-0">
          <div className="font-display text-[15px] leading-none text-[var(--ink-50)] tracking-wide">DevAI</div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--ink-500)] mt-1">Mission control</div>
        </div>
      </div>

      {/* Cmd-K hint — clicking it opens the palette via the global event. */}
      <button
        type="button"
        onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
        className="mx-3 mt-3 flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] hover:border-[var(--surface-border-strong)] text-xs text-[var(--ink-300)] transition-colors"
      >
        <Search className="w-3.5 h-3.5" />
        <span className="flex-1 text-left">Jump to…</span>
        <span className="font-mono text-[10px] px-1.5 py-0.5 rounded bg-[var(--surface-3)] text-[var(--ink-300)]">⌘K</span>
      </button>

      {/* Role chip */}
      <div className="px-3 mt-4">
        <div className="label-eyebrow mb-1.5 px-1">Role</div>
        <div className="flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm">
          <span className="dot dot-ok" />
          <span className="flex-1 text-[var(--ink-100)]">Administrator</span>
        </div>
      </div>

      {/* Nav groups */}
      <div className="px-2 mt-4 flex-1 overflow-y-auto">
        <NavSection items={TOP} pathname={pathname} label="Fleet" />
        <NavSection items={MID} pathname={pathname} label="Platform" />
      </div>

      {/* Meta: theme toggle */}
      <div className="px-2">
        <NavSection items={BOTTOM} pathname={pathname} />
        <button
          type="button"
          onClick={toggleDark}
          className="w-full mt-1 flex items-center gap-2.5 px-2 py-1.5 rounded-md text-sm text-[var(--ink-300)] hover:bg-white/5 transition-colors"
        >
          {dark ? <Moon className="w-4 h-4 shrink-0" /> : <Sun className="w-4 h-4 shrink-0" />}
          <span>{dark ? "Dark" : "Light"}</span>
        </button>
      </div>

      {/* Primary CTA */}
      <div className="px-3 py-3 border-t border-[var(--surface-border)]">
        <button
          type="button"
          onClick={newTask}
          className="w-full inline-flex items-center justify-center gap-2 px-3 py-2 rounded-md bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium shadow-sm transition-colors"
        >
          <Plus className="w-4 h-4" />
          New task
        </button>
      </div>
    </aside>
  );
}

function NavSection({
  items,
  pathname,
  label,
}: {
  items: NavItem[];
  pathname: string;
  label?: string;
}) {
  return (
    <nav className="mb-4">
      {label && <div className="px-2 mb-1.5 label-eyebrow">{label}</div>}
      <ul className="space-y-0.5">
        {items.map(({ href, label: l, Icon, description, badge }) => {
          const active = isActive(href, pathname);
          return (
            <li key={href}>
              <Link
                href={href}
                title={description}
                className={clsx(
                  "flex items-center gap-2.5 px-2 py-1.5 rounded-md text-sm transition-colors group",
                  active
                    ? "bg-indigo-500/15 text-[var(--ink-50)] font-medium ring-1 ring-inset ring-indigo-500/30"
                    : "text-[var(--ink-300)] hover:bg-white/5 hover:text-[var(--ink-100)]"
                )}
              >
                <Icon className={clsx("w-4 h-4 shrink-0", active ? "text-indigo-300" : "text-[var(--ink-500)] group-hover:text-[var(--ink-300)]")} />
                <span className="truncate flex-1">{l}</span>
                {badge && (
                  <span className="font-mono text-[10px] text-[var(--ink-500)] px-1.5 py-0.5 rounded bg-[var(--surface-2)] border border-[var(--surface-border)]">
                    {badge}
                  </span>
                )}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
