"use client";

/**
 * In-context guidance surfaces.
 *
 *   <GuidancePanel id="workflows" />   a dismissible explainer card. Reads the
 *                                      matching entry from help-content.ts. The
 *                                      dismissal persists per id in localStorage
 *                                      (devai-guidance:<id>), so once a user
 *                                      gets it, it stays out of the way.
 *
 *   <HelpPopover term="dynamic" />     a small "?" affordance that reveals a
 *                                      tooltip explaining one term. Keyboard +
 *                                      pointer accessible; closes on Esc / blur.
 *
 * Both read from the typed maps in lib/help-content.ts — never inline copy here,
 * so the wording stays consistent across every page.
 *
 * Visual language: the shared tokens + custom classes (panel, label-eyebrow,
 * btn-ghost). No new colours, no new layout primitives.
 */

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { clsx } from "clsx";
import { HelpCircle, Info, X } from "lucide-react";
import { getGuidance, getHelpTerm } from "@/lib/help-content";

const DISMISS_PREFIX = "devai-guidance:";

// ─────────────────────────────────────────────────────────────────────────
// GuidancePanel
// ─────────────────────────────────────────────────────────────────────────

export function GuidancePanel({
  id,
  className,
  /** When true the card cannot be dismissed (used in modals/help drawers). */
  persistent = false,
}: {
  id: string;
  className?: string;
  persistent?: boolean;
}) {
  const entry = getGuidance(id);
  // null = not yet resolved (avoids SSR/CSR flash); true/false once known.
  const [dismissed, setDismissed] = useState<boolean | null>(persistent ? false : null);

  useEffect(() => {
    if (persistent) return;
    if (typeof window === "undefined") return;
    setDismissed(window.localStorage.getItem(DISMISS_PREFIX + id) === "1");
  }, [id, persistent]);

  if (!entry) return null;
  // Until we know the stored state, render nothing to avoid a flash-then-hide.
  if (dismissed === null || dismissed) return null;

  function dismiss() {
    setDismissed(true);
    try {
      window.localStorage.setItem(DISMISS_PREFIX + id, "1");
    } catch {
      /* storage may be unavailable — dismissal is best-effort */
    }
  }

  return (
    <aside
      className={clsx("panel p-4 flex gap-3", className)}
      style={{ background: "var(--info-soft-bg)", borderColor: "var(--info-soft-bd)" }}
      role="note"
      aria-label={entry.title}
    >
      <Info className="w-4 h-4 mt-0.5 shrink-0" style={{ color: "var(--info)" }} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-3">
          <h3 className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
            {entry.title}
          </h3>
          {!persistent && (
            <button
              type="button"
              onClick={dismiss}
              className="btn-ghost -mr-1.5 -mt-1 p-1.5 shrink-0"
              aria-label="Dismiss guidance"
              title="Dismiss"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
        <div className="mt-1.5 space-y-1.5">
          {entry.body.map((p, i) => (
            <p key={i} className="text-[13px] leading-relaxed" style={{ color: "var(--ink-soft)" }}>
              {p}
            </p>
          ))}
        </div>
        {entry.links && entry.links.length > 0 && (
          <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1">
            {entry.links.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                className="text-[12px] font-medium underline-offset-2 hover:underline"
                style={{ color: "var(--accent)" }}
              >
                {l.label}
              </Link>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// HelpPopover
// ─────────────────────────────────────────────────────────────────────────

export function HelpPopover({
  term,
  className,
  /** Override the trigger label (defaults to the "?" icon only). */
  label,
}: {
  term: string;
  className?: string;
  label?: string;
}) {
  const entry = getHelpTerm(term);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!entry) return null;

  return (
    <span ref={wrapRef} className={clsx("relative inline-flex items-center", className)}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 align-middle rounded transition-colors"
        style={{ color: "var(--ink-muted)" }}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`What is ${entry.label}?`}
      >
        <HelpCircle className="w-3.5 h-3.5" aria-hidden />
        {label && <span className="text-[12px]">{label}</span>}
      </button>
      {open && (
        <span
          id={panelId}
          role="tooltip"
          className="absolute left-0 top-full z-50 mt-1.5 w-72 panel-raised p-3 block"
          style={{ background: "var(--surface-raised)" }}
        >
          <span className="label-eyebrow block">{entry.label}</span>
          <span
            className="mt-1.5 block text-[12.5px] leading-relaxed"
            style={{ color: "var(--ink-soft)" }}
          >
            {entry.summary}
          </span>
          {entry.points && entry.points.length > 0 && (
            <ul className="mt-2 space-y-1">
              {entry.points.map((pt, i) => (
                <li
                  key={i}
                  className="text-[12px] leading-snug pl-3 relative"
                  style={{ color: "var(--ink-muted)" }}
                >
                  <span
                    className="dot dot-muted absolute left-0 top-[7px]"
                    aria-hidden
                  />
                  {pt}
                </li>
              ))}
            </ul>
          )}
        </span>
      )}
    </span>
  );
}
