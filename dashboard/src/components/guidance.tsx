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
 *   <GuidanceInfo id="workflows" />    the same page explainer behind an "i"
 *                                      icon. Always available — this is how a
 *                                      user gets the help back after dismissing
 *                                      the panel.
 *
 *   <HelpPopover term="dynamic" />     an "i" affordance explaining one term.
 *
 *   <InfoHint title=… body=… />        the shared primitive both sit on, for
 *                                      copy that isn't in help-content.ts (nav
 *                                      rows, section headers).
 *
 * The content components read from the typed maps in lib/help-content.ts —
 * never inline copy here, so the wording stays consistent across every page.
 *
 * Visual language: the shared tokens + custom classes (panel, label-eyebrow,
 * btn-ghost). No new colours, no new layout primitives.
 */

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { clsx } from "clsx";
import { Info, X } from "lucide-react";
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
// InfoHint — the shared "i" affordance
// ─────────────────────────────────────────────────────────────────────────

/** Milliseconds of hover before opening / after leaving. Prevents the popover
 *  flickering as the pointer crosses a row on its way somewhere else. */
const OPEN_DELAY = 120;
const CLOSE_DELAY = 180;

type Placement = "bottom-start" | "right";

export function InfoHint({
  title,
  body,
  points,
  links,
  placement = "bottom-start",
  className,
  label,
  ariaLabel,
}: {
  title: string;
  body: string[];
  points?: string[];
  links?: HelpLinkish[];
  placement?: Placement;
  className?: string;
  /** Optional visible text next to the icon. */
  label?: string;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  // Measured from the trigger and applied as position:fixed, so the popover is
  // never clipped by an ancestor's overflow — the left nav is a scroll container.
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const panelId = useId();

  const clearTimer = () => {
    if (timer.current) clearTimeout(timer.current);
  };

  const place = useCallback(() => {
    const el = wrapRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const W = 288; // w-72
    const GAP = 8;
    let left = placement === "right" ? r.right + GAP : r.left;
    let top = placement === "right" ? r.top : r.bottom + GAP;
    // Keep it on screen — flip/clamp rather than overflow the viewport.
    if (left + W > window.innerWidth - GAP) left = Math.max(GAP, window.innerWidth - W - GAP);
    if (top > window.innerHeight - 140) top = Math.max(GAP, window.innerHeight - 160);
    setPos({ top, left });
  }, [placement]);

  const show = useCallback(
    (delay: number) => {
      clearTimer();
      timer.current = setTimeout(() => {
        place();
        setOpen(true);
      }, delay);
    },
    [place],
  );

  const hide = useCallback((delay: number) => {
    clearTimer();
    timer.current = setTimeout(() => setOpen(false), delay);
  }, []);

  useEffect(() => clearTimer, []);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    // A fixed popover doesn't travel with its trigger, so dismiss on scroll.
    function onScroll() {
      setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDocClick);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDocClick);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open]);

  return (
    <span
      ref={wrapRef}
      className={clsx("relative inline-flex items-center", className)}
      onMouseEnter={() => show(OPEN_DELAY)}
      onMouseLeave={() => hide(CLOSE_DELAY)}
    >
      <button
        type="button"
        // Click still works: touch devices have no hover, and it pins the
        // popover open for anyone reading a longer explanation.
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          if (open) {
            clearTimer();
            setOpen(false);
          } else {
            show(0);
          }
        }}
        onFocus={() => show(0)}
        onBlur={() => hide(CLOSE_DELAY)}
        className="inline-flex items-center gap-1 align-middle rounded transition-colors hover:opacity-100"
        style={{ color: "var(--ink-muted)" }}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={ariaLabel ?? `What is ${title}?`}
      >
        <Info className="w-3.5 h-3.5" aria-hidden />
        {label && <span className="text-[12px]">{label}</span>}
      </button>
      {open && pos && (
        <span
          id={panelId}
          role="tooltip"
          className="fixed z-[60] w-72 panel-raised p-3 block"
          style={{ top: pos.top, left: pos.left, background: "var(--surface-raised)" }}
          onMouseEnter={clearTimer}
          onMouseLeave={() => hide(CLOSE_DELAY)}
        >
          <span className="label-eyebrow block">{title}</span>
          {body.map((p, i) => (
            <span
              key={i}
              className="mt-1.5 block text-[12.5px] leading-relaxed"
              style={{ color: "var(--ink-soft)" }}
            >
              {p}
            </span>
          ))}
          {points && points.length > 0 && (
            <ul className="mt-2 space-y-1">
              {points.map((pt, i) => (
                <li
                  key={i}
                  className="text-[12px] leading-snug pl-3 relative"
                  style={{ color: "var(--ink-muted)" }}
                >
                  <span className="dot dot-muted absolute left-0 top-[7px]" aria-hidden />
                  {pt}
                </li>
              ))}
            </ul>
          )}
          {links && links.length > 0 && (
            <span className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1">
              {links.map((l) => (
                <Link
                  key={l.href}
                  href={l.href}
                  className="text-[12px] font-medium underline-offset-2 hover:underline"
                  style={{ color: "var(--accent)" }}
                >
                  {l.label}
                </Link>
              ))}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

type HelpLinkish = { label: string; href: string };

// ─────────────────────────────────────────────────────────────────────────
// HelpPopover / GuidanceInfo — content-bound wrappers
// ─────────────────────────────────────────────────────────────────────────

export function HelpPopover({
  term,
  className,
  /** Override the trigger label (defaults to the icon only). */
  label,
}: {
  term: string;
  className?: string;
  label?: string;
}) {
  const entry = getHelpTerm(term);
  if (!entry) return null;
  return (
    <InfoHint
      title={entry.label}
      body={[entry.summary]}
      points={entry.points}
      className={className}
      label={label}
    />
  );
}

/** The page explainer, reachable after the GuidancePanel has been dismissed. */
export function GuidanceInfo({ id, className }: { id: string; className?: string }) {
  const entry = getGuidance(id);
  if (!entry) return null;
  return (
    <InfoHint
      title={entry.title}
      body={entry.body}
      links={entry.links}
      className={className}
      ariaLabel="About this page"
    />
  );
}
