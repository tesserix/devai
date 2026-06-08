"use client";

/**
 * Breadcrumbs — a simple, accessible trail for the disambiguated IA.
 *
 * The workflows / blueprints / runs / board areas now cross-link, so every
 * deep page gets a "Workflows / alm-pipeline / Run a1b2c3" trail back to its
 * parents. The last crumb is the current page (not a link).
 *
 * Pure presentational. Uses the shared tokens + label-eyebrow density.
 */

import Link from "next/link";
import { ChevronRight } from "lucide-react";

export interface Crumb {
  label: string;
  /** Omit href on the current (last) crumb. */
  href?: string;
}

export function Breadcrumbs({
  items,
  className,
}: {
  items: Crumb[];
  className?: string;
}) {
  if (items.length === 0) return null;

  return (
    <nav aria-label="Breadcrumb" className={className}>
      <ol className="flex items-center gap-1.5 flex-wrap text-[12px]">
        {items.map((c, i) => {
          const isLast = i === items.length - 1;
          return (
            <li key={`${c.label}-${i}`} className="flex items-center gap-1.5 min-w-0">
              {c.href && !isLast ? (
                <Link
                  href={c.href}
                  className="truncate transition-colors hover:underline underline-offset-2"
                  style={{ color: "var(--ink-muted)" }}
                >
                  {c.label}
                </Link>
              ) : (
                <span
                  className="truncate"
                  style={{ color: isLast ? "var(--ink-strong)" : "var(--ink-muted)" }}
                  aria-current={isLast ? "page" : undefined}
                >
                  {c.label}
                </span>
              )}
              {!isLast && (
                <ChevronRight
                  className="w-3 h-3 shrink-0"
                  style={{ color: "var(--ink-disabled)" }}
                  aria-hidden
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
