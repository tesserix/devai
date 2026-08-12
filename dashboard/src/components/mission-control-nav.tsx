"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { clsx } from "clsx";
import {
  BarChart3,
  Box,
  Boxes,
  FolderGit2,
  FolderKanban,
  ChevronRight,
  Layers,
  Lightbulb,
  ListChecks,
  LogOut,
  MessageSquareText,
  Moon,
  PackageOpen,
  Plus,
  Radio,
  ScrollText,
  Search,
  Settings,
  ShieldHalf,
  Sparkles,
  Sun,
  Users,
  Workflow,
  Wrench,
  X,
} from "lucide-react";
import { InfoHint } from "@/components/guidance";

/**
 * Fiber-style mission-control left nav.
 *
 * Every colour / surface / border reads from the design tokens in
 * globals.css. Neither the sidebar nor any nav row hardcodes a
 * Tailwind grayscale utility — the same component renders cleanly
 * in both dark and light modes without conditional rendering.
 *
 * Sections read top-to-bottom as a journey: Get started → Your work →
 * Building blocks → Monitoring. See the per-section comments below.
 */

type NavItem = {
  href: string;
  label: string;
  Icon: typeof Box;
  description?: string;
  badge?: string;
};

// Sections are ordered by what a user is trying to DO, not by which backend
// owns the page, and every label is the plain-English name for the thing —
// users consistently could not tell what "Fleet", "Registry" or "Gateway" were.
// Hrefs are unchanged, so bookmarks and deep links still resolve.

// 1. GET STARTED — the happy path for a first run, in order.
const START: NavItem[] = [
  { href: "/repos", label: "Repositories", Icon: FolderGit2, description: "Step 1 — connect the repos DevAI may work on" },
  { href: "/workflows", label: "Workflows", Icon: Workflow, description: "Step 2 — pick a workflow and run it" },
  { href: "/compose", label: "Describe a task", Icon: Sparkles, description: "Or just say what you want in plain English" },
];

// 2. YOUR WORK — where a run in flight is watched and tracked.
const WORK: NavItem[] = [
  { href: "/", label: "Live run", Icon: Boxes, description: "Watch the current run, stage by stage" },
  { href: "/runs", label: "Run history", Icon: ListChecks, description: "Every run, past and present" },
  { href: "/board", label: "Task board", Icon: FolderKanban, description: "Your GitHub issues as a Kanban" },
];

// 3. BUILDING BLOCKS — the shared registry, one page per artifact kind.
// Optional for most users; the defaults cover the whole lifecycle.
const BLOCKS: NavItem[] = [
  { href: "/agents", label: "Agents", Icon: Users, description: "The worker roles a workflow can assign" },
  { href: "/skills", label: "Skills", Icon: Lightbulb, description: "Know-how you can attach to an agent" },
  { href: "/tools", label: "Tools", Icon: Wrench, description: "External things an agent can call" },
  { href: "/prompts", label: "Prompts", Icon: MessageSquareText, description: "Reusable instruction templates" },
  { href: "/blueprint", label: "Workflow designer", Icon: Layers, description: "Build and publish your own workflows" },
  { href: "/registry", label: "Browse all", Icon: PackageOpen, description: "Search everything in one catalog" },
];

// 4. MONITORING — is it working, what did it cost, and production health.
const MONITOR: NavItem[] = [
  { href: "/analytics", label: "Analytics", Icon: BarChart3, description: "Success rates, agent load and LLM cost" },
  { href: "/logs", label: "Logs", Icon: ScrollText, description: "Live output, reliability targets and archive" },
  { href: "/gateway", label: "Service health", Icon: Radio, description: "Is the DevAI platform itself reachable?" },
  { href: "/sre-studio", label: "SRE Studio", Icon: ShieldHalf, description: "Rules the SRE agents use to watch production" },
];

const BOTTOM: NavItem[] = [
  { href: "/settings", label: "Settings", Icon: Settings },
];

// Answers "why are these four things grouped together?" — the question the
// section names alone can't.
const SECTION_HINTS: Record<string, string> = {
  start: "Everything you need before a first run: the repos DevAI may touch, and the two ways to kick off work — pick a ready-made workflow, or describe the task in plain English.",
  work: "Work that is already underway. Live run follows the current one stage by stage; Run history is the full record; Task board mirrors your GitHub issues.",
  blocks: "The reusable parts a workflow is assembled from. You can ignore all of it — the built-in defaults already cover the whole lifecycle. Open these only to add your own.",
  monitor: "How things are going, after the fact. Success rates and cost, raw log output, whether the DevAI platform itself is reachable, and the rules the SRE agents apply to production.",
};

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

  // The "New task" CTA opens the lifecycle home (Workflows), where the
  // blueprint picker + trigger dialog live — NOT the old /control stub (DASH-9).
  function newTask() {
    router.push("/workflows?action=new");
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
        <NavSection items={START} pathname={pathname} label="Get started" hint={SECTION_HINTS.start} />
        <NavSection items={WORK} pathname={pathname} label="Your work" hint={SECTION_HINTS.work} />
        <NavSection items={BLOCKS} pathname={pathname} label="Building blocks" hint={SECTION_HINTS.blocks} />
        <NavSection items={MONITOR} pathname={pathname} label="Monitoring" hint={SECTION_HINTS.monitor} />
      </div>

      {/* Meta + theme toggle + sign out */}
      <div className="px-2 pb-1">
        <NavSection items={BOTTOM} pathname={pathname} />
        <button type="button" onClick={toggleDark} className="btn-ghost !justify-start w-full mt-0.5 !text-[13px]">
          {dark ? <Moon className="w-4 h-4" /> : <Sun className="w-4 h-4" />}
          <span>{dark ? "Dark" : "Light"}</span>
        </button>
        {/* Sign out — clears the auth-bff session cookie and returns to login. */}
        <a href="/auth/logout" className="btn-ghost !justify-start w-full mt-0.5 !text-[13px]">
          <LogOut className="w-4 h-4" />
          <span>Sign out</span>
        </a>
      </div>

      {/* Primary CTA — ink button (mark8ly admin pattern). */}
      <div className="px-3 py-3 border-t" style={{ borderColor: "var(--border-subtle)" }}>
        <button type="button" onClick={newTask} className="btn-primary w-full !py-2 !text-sm">
          <Plus className="w-3.5 h-3.5" />
          Start a run
        </button>
      </div>
    </aside>
  );
}

function NavSection({
  items,
  pathname,
  label,
  hint,
}: {
  items: NavItem[];
  pathname: string;
  label?: string;
  hint?: string;
}) {
  // Labelled sections are collapsible; the collapsed state persists per
  // section in localStorage. Unlabelled sections (e.g. the bottom row) are
  // always shown. A section that contains the active route is force-expanded
  // so you never lose sight of where you are.
  const storageKey = label ? `devai-nav-collapsed:${label}` : "";
  const containsActive = items.some((it) => isActive(it.href, pathname));
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    if (!storageKey) return;
    setCollapsed(localStorage.getItem(storageKey) === "1");
  }, [storageKey]);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    if (storageKey) localStorage.setItem(storageKey, next ? "1" : "0");
  };

  const showItems = !label || !collapsed || containsActive;

  return (
    <nav className="mb-4">
      {label && (
        // The hint is a sibling of the toggle, not a child — a button inside a
        // button is invalid and swallows the inner click.
        <div className="flex items-center gap-1 px-2 mb-1.5 group">
          <button
            type="button"
            onClick={toggle}
            aria-expanded={showItems}
            className="flex flex-1 items-center gap-1 min-w-0"
            style={{ color: "var(--ink-muted)" }}
            title={collapsed ? `Expand ${label}` : `Collapse ${label}`}
          >
            <ChevronRight
              className="w-3 h-3 shrink-0 transition-transform"
              style={{ transform: showItems ? "rotate(90deg)" : "rotate(0deg)" }}
            />
            <span className="label-eyebrow">{label}</span>
          </button>
          {hint && (
            <InfoHint
              title={label}
              body={[hint]}
              placement="right"
              ariaLabel={`About ${label}`}
              className="opacity-0 group-hover:opacity-100 focus-within:opacity-100 shrink-0"
            />
          )}
        </div>
      )}
      {showItems && (
      <ul className="space-y-0.5">
        {items.map(({ href, label: l, Icon, description, badge }) => {
          const active = isActive(href, pathname);
          return (
            <li key={href}>
              <Link
                href={href}
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
                {description && (
                  // Appears on row hover/focus so the rail stays calm, but is
                  // always in the tab order for keyboard and screen readers.
                  <InfoHint
                    title={l}
                    body={[description]}
                    placement="right"
                    ariaLabel={`What is ${l}?`}
                    className="opacity-0 group-hover:opacity-100 focus-within:opacity-100 shrink-0"
                  />
                )}
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
      )}
    </nav>
  );
}
