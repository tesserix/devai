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
  FolderGit2,
  GitBranch,
  Layers,
  Lightbulb,
  LineChart,
  Moon,
  PackageOpen,
  Plus,
  Radio,
  Search,
  Settings,
  ShieldHalf,
  Sparkles,
  Sun,
  Users,
  Wrench,
  X,
} from "lucide-react";

/**
 * Fiber-style mission-control left nav.
 *
 * Every colour / surface / border reads from the design tokens in
 * globals.css. Neither the sidebar nor any nav row hardcodes a
 * Tailwind grayscale utility — the same component renders cleanly
 * in both dark and light modes without conditional rendering.
 *
 * Visual map (matches the screenshot we anchored on):
 *
 *   logo + "Mission control" eyebrow
 *   ┌──────────────────────────┐
 *   │  🔍 Jump to…       ⌘K    │  command palette trigger
 *   └──────────────────────────┘
 *   ROLE
 *   • Administrator           ▾
 *
 *   FLEET
 *   ▣ Fleet                [1]
 *   ‖ Workflows
 *   ▢ Blueprint
 *   ⛨ Agents
 *   ⌽ Memory
 *
 *   PLATFORM
 *   ▤ Registry
 *   ▦ Catalog
 *   ⇆ Control
 *   ⥤ Analytics
 *   ⚒ Tools
 *
 *   ⚙ Settings
 *   ☾ Dark   (toggle)
 *   ─────────────────────────
 *   [   + New task   ]   primary CTA, pinned bottom
 */

type NavItem = {
  href: string;
  label: string;
  Icon: typeof Box;
  description?: string;
  badge?: string;
};

const TOP: NavItem[] = [
  { href: "/compose", label: "Compose", Icon: Sparkles, description: "Cursor-style agent composer" },
  { href: "/", label: "Fleet", Icon: Boxes, description: "Active pipeline runs" },
  { href: "/workflows", label: "Workflows", Icon: GitBranch, description: "Cross-functional gates" },
  { href: "/blueprint", label: "Blueprint", Icon: Layers, description: "DAG of stages" },
  { href: "/agents", label: "Agents", Icon: Users, description: "Catalogued agents" },
  { href: "/skills", label: "Skills", Icon: Lightbulb, description: "Reusable capabilities" },
  { href: "/memory", label: "Memory", Icon: BrainCog, description: "Episodic + semantic" },
];

const MID: NavItem[] = [
  { href: "/repos", label: "Repos", Icon: FolderGit2, description: "Onboard org repositories" },
  { href: "/registry", label: "Registry", Icon: PackageOpen, description: "Skills · Prompts · MCP · Agents" },
  { href: "/gateway", label: "Gateway", Icon: Radio, description: "Agent Gateway + LLM proxy health" },
  { href: "/catalog", label: "Catalog", Icon: Database, description: "Resolved capability map" },
  { href: "/control", label: "Control", Icon: ChevronsLeftRight, description: "Manual pause / takeover" },
  { href: "/analytics", label: "Analytics", Icon: LineChart, description: "Cost + duration trends" },
  { href: "/tools", label: "Tools", Icon: Wrench, description: "Custom MCP tools" },
];

const BOTTOM: NavItem[] = [
  { href: "/settings", label: "Settings", Icon: Settings },
];

function isActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

export function MissionControlNav({
  open = false,
  onClose,
}: {
  open?: boolean;
  onClose?: () => void;
} = {}) {
  const pathname = usePathname();
  const router = useRouter();
  const [dark, setDark] = useState(false);

  // Read the resolved theme that the inline-init script applied. Doing
  // it in useEffect rather than as a default prevents the SSR/CSR
  // mismatch (window doesn't exist server-side).
  useEffect(() => {
    if (typeof window === "undefined") return;
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  // Close the mobile drawer on navigation.
  useEffect(() => {
    onClose?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  function toggleDark() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    window.localStorage.setItem("devai-theme", next ? "dark" : "light");
  }

  function newTask() {
    router.push("/control?action=new");
  }

  return (
    <aside
      className={clsx(
        // Below md: a fixed slide-in drawer over the content. md+: a static
        // in-flow rail (translate reset, no z-index).
        "w-60 shrink-0 flex flex-col border-r fixed inset-y-0 left-0 z-40 transition-transform duration-200 ease-out md:static md:z-auto md:translate-x-0",
        open ? "translate-x-0" : "-translate-x-full",
      )}
      style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
    >
      {/* Logo + tag. Logomark is ink (near-black) on paper — same
       * editorial treatment as the mark8ly admin. The previous
       * indigo gradient read like a SaaS app icon; this reads like
       * a tool. */}
      <div className="px-4 pt-4 pb-2 flex items-center gap-2.5">
        <span
          className="inline-flex w-8 h-8 rounded-md items-center justify-center shadow-sm shrink-0"
          style={{ background: "var(--primary)", color: "var(--primary-ink)" }}
        >
          <ShieldHalf className="w-4 h-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div
            className="font-serif text-[15px] leading-none"
            style={{ color: "var(--ink-strong)" }}
          >
            DevAI
          </div>
          <div className="label-eyebrow mt-1.5">Mission control</div>
        </div>
        {/* Close (mobile drawer only). */}
        <button
          type="button"
          onClick={onClose}
          className="btn-ghost md:hidden -mr-1.5 p-2"
          aria-label="Close menu"
        >
          <X className="w-[18px] h-[18px]" />
        </button>
      </div>

      {/* ⌘K palette trigger */}
      <button
        type="button"
        onClick={() =>
          window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))
        }
        className="btn-secondary mx-3 mt-3 !justify-between !py-1.5"
      >
        <span className="inline-flex items-center gap-2">
          <Search className="w-3.5 h-3.5" style={{ color: "var(--ink-muted)" }} />
          <span style={{ color: "var(--ink-soft)" }}>Jump to…</span>
        </span>
        <kbd
          className="font-mono text-[10px] px-1.5 py-0.5 rounded"
          style={{ background: "var(--surface-muted)", color: "var(--ink-muted)" }}
        >
          ⌘K
        </kbd>
      </button>

      {/* Role chip */}
      <div className="px-3 mt-4">
        <div className="label-eyebrow mb-1.5 px-1">Role</div>
        <div
          className="flex items-center gap-2 px-2.5 py-1.5 rounded-md text-sm"
          style={{
            background: "var(--surface-muted)",
            border: "1px solid var(--border-subtle)",
            color: "var(--ink)",
          }}
        >
          <span className="dot dot-ok" />
          <span className="flex-1">Administrator</span>
        </div>
      </div>

      {/* Nav groups */}
      <div className="px-2 mt-4 flex-1 overflow-y-auto">
        <NavSection items={TOP} pathname={pathname} label="Fleet" />
        <NavSection items={MID} pathname={pathname} label="Platform" />
      </div>

      {/* Meta + theme toggle */}
      <div className="px-2 pb-1">
        <NavSection items={BOTTOM} pathname={pathname} />
        <button type="button" onClick={toggleDark} className="btn-ghost !justify-start w-full mt-0.5 !text-[13px]">
          {dark ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
          <span>{dark ? "Dark" : "Light"}</span>
        </button>
      </div>

      {/* Primary CTA — ink button (mark8ly admin pattern). */}
      <div className="px-3 py-3 border-t" style={{ borderColor: "var(--border-subtle)" }}>
        <button type="button" onClick={newTask} className="btn-primary w-full !py-2 !text-sm">
          <Plus className="w-3.5 h-3.5" />
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
                  "flex items-center gap-2.5 px-2 py-1.5 rounded-md text-sm transition-colors group"
                )}
                style={{
                  // Active state uses an ink-soft tint (mark8ly admin
                  // pattern) — a calm filled rail. The previous accent-
                  // soft + ring read too prominently for a side rail.
                  background: active ? "var(--surface-hover)" : "transparent",
                  color: active ? "var(--ink-strong)" : "var(--ink-soft)",
                  fontWeight: active ? 600 : 400,
                }}
                onMouseEnter={(e) => {
                  if (!active) e.currentTarget.style.background = "var(--surface-muted)";
                }}
                onMouseLeave={(e) => {
                  if (!active) e.currentTarget.style.background = "transparent";
                }}
              >
                <Icon
                  className="w-4 h-4 shrink-0"
                  style={{ color: active ? "var(--ink-strong)" : "var(--ink-muted)" }}
                />
                <span className="truncate flex-1">{l}</span>
                {badge && (
                  <span
                    className="font-mono text-[10px] px-1.5 py-0.5 rounded"
                    style={{
                      background: "var(--surface-muted)",
                      color: "var(--ink-muted)",
                      border: "1px solid var(--border-subtle)",
                    }}
                  >
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
