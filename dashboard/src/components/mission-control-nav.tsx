"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
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
  PackageOpen,
  Settings,
  ShieldHalf,
  Users,
  Wrench,
} from "lucide-react";

/**
 * Fiber-style mission-control left nav.
 *
 * Top section is the high-traffic surface (Fleet / Workflows / Blueprint
 * / Agents). Bottom section is configuration + meta. We don't ship every
 * Fiber panel today — Memory / Catalog / Control / Analytics / Tools are
 * placeholders so the nav structure is in place and individual panels
 * can land incrementally without re-shuffling.
 */
type NavItem = {
  href: string;
  label: string;
  Icon: typeof Box;
  description?: string;
};

const TOP: NavItem[] = [
  { href: "/", label: "Fleet", Icon: Boxes, description: "Active pipeline runs" },
  { href: "/workflows", label: "Workflows", Icon: GitBranch, description: "Cross-functional gates" },
  { href: "/blueprint", label: "Blueprint", Icon: Layers, description: "DAG of stages" },
  { href: "/agents", label: "Agents", Icon: Users, description: "Catalogued agents" },
  { href: "/memory", label: "Memory", Icon: BrainCog, description: "Episodic + semantic" },
];

const MID: NavItem[] = [
  { href: "/registry", label: "Registry", Icon: PackageOpen, description: "Skills / prompts / MCP / agents" },
  { href: "/catalog", label: "Catalog", Icon: Database, description: "Resolved capability map" },
  { href: "/control", label: "Control", Icon: ChevronsLeftRight, description: "Manual pause / takeover" },
  { href: "/analytics", label: "Analytics", Icon: LineChart, description: "Cost + duration trends" },
  { href: "/tools", label: "Tools", Icon: Wrench, description: "Per-role MCP allow-lists" },
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

  return (
    <aside className="w-56 shrink-0 border-r border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-950 px-3 py-4 flex flex-col">
      <div className="px-2 mb-6">
        <div className="flex items-center gap-2">
          <span className="inline-flex w-7 h-7 rounded-lg bg-indigo-600 text-white items-center justify-center">
            <ShieldHalf className="w-4 h-4" />
          </span>
          <div>
            <div className="text-sm font-semibold text-gray-900 dark:text-gray-100 leading-tight">DevAI</div>
            <div className="text-[10px] uppercase tracking-wider text-gray-500 dark:text-gray-500">Mission Control</div>
          </div>
        </div>
      </div>

      <NavSection items={TOP} pathname={pathname} label="Fleet" />
      <NavSection items={MID} pathname={pathname} label="Platform" />
      <div className="flex-1" />
      <NavSection items={BOTTOM} pathname={pathname} />
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
    <nav className="mb-5">
      {label && (
        <div className="px-2 mb-2 text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-600 font-medium">
          {label}
        </div>
      )}
      <ul className="space-y-0.5">
        {items.map(({ href, label: l, Icon, description }) => {
          const active = isActive(href, pathname);
          return (
            <li key={href}>
              <Link
                href={href}
                title={description}
                className={clsx(
                  "flex items-center gap-2.5 px-2 py-1.5 rounded-md text-sm transition-colors",
                  active
                    ? "bg-indigo-50 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-200 font-medium"
                    : "text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50"
                )}
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span className="truncate">{l}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
