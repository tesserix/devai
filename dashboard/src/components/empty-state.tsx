"use client";

/**
 * EmptyState — one consistent empty/zero-data surface for every page.
 *
 * Replaces the scattered "No pipeline runs yet" / centered-grey-text blocks
 * with a single calm, professional layout: a quiet icon chip, a title, a line
 * of explanation, and an optional primary action.
 *
 * Pure presentational. Uses the shared tokens + btn-primary/btn-secondary.
 */

import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import { Inbox } from "lucide-react";

interface ActionLink {
  label: string;
  href: string;
}
interface ActionButton {
  label: string;
  onClick: () => void;
}

export function EmptyState({
  Icon = Inbox,
  title,
  description,
  action,
  secondary,
  /** Tighter padding for in-panel use (e.g. a sidebar list). */
  compact = false,
}: {
  Icon?: LucideIcon;
  title: string;
  description?: string;
  /** Primary CTA — either a link or a button. */
  action?: ActionLink | ActionButton;
  /** Optional quieter secondary action. */
  secondary?: ActionLink | ActionButton;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center ${
        compact ? "py-8 px-4" : "py-16 px-6"
      }`}
    >
      <span
        className="inline-flex items-center justify-center rounded-xl mb-3"
        style={{
          width: compact ? 36 : 44,
          height: compact ? 36 : 44,
          background: "var(--surface-muted)",
          border: "1px solid var(--border-subtle)",
          color: "var(--ink-muted)",
        }}
      >
        <Icon className={compact ? "w-4 h-4" : "w-5 h-5"} aria-hidden />
      </span>
      <h3 className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
        {title}
      </h3>
      {description && (
        <p
          className="mt-1.5 text-[13px] leading-relaxed max-w-sm"
          style={{ color: "var(--ink-muted)" }}
        >
          {description}
        </p>
      )}
      {(action || secondary) && (
        <div className="mt-4 flex items-center gap-2">
          {action && renderAction(action, "btn-primary !py-1.5 !text-sm")}
          {secondary && renderAction(secondary, "btn-secondary !py-1.5 !text-sm")}
        </div>
      )}
    </div>
  );
}

function renderAction(a: ActionLink | ActionButton, className: string) {
  if ("href" in a) {
    return (
      <Link href={a.href} className={className}>
        {a.label}
      </Link>
    );
  }
  return (
    <button type="button" onClick={a.onClick} className={className}>
      {a.label}
    </button>
  );
}
