"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Search } from "lucide-react";
import { placeMenu, type MenuPlacement } from "@/components/ui/select-position";

/**
 * The one dropdown DevAI uses.
 *
 * A native <select> renders an OS menu we cannot theme, truncates the
 * descriptions that tell two options apart, and becomes unusable past a dozen
 * entries. This keeps the same keyboard contract (↑ ↓ Enter Esc, type to
 * filter) while showing a description, a badge and a group per option.
 *
 * The menu renders in a portal: inside a scrolling modal or a card with
 * overflow, an in-flow menu is clipped by its ancestor and the options below
 * the fold cannot be reached at all.
 */

export type SelectOption = {
  value: string;
  label: string;
  description?: string;
  badge?: string;
  group?: string;
  disabled?: boolean;
};

const SEARCH_FROM = 8;

export function Select({
  value,
  onChange,
  options,
  placeholder = "Choose…",
  icon,
  mono = false,
  searchable,
  disabled = false,
  emptyLabel = "Nothing to choose from.",
  className = "",
  ariaLabel,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  icon?: ReactNode;
  mono?: boolean;
  searchable?: boolean;
  disabled?: boolean;
  emptyLabel?: string;
  className?: string;
  ariaLabel?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [menu, setMenu] = useState<MenuPlacement | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const withSearch = searchable ?? options.length >= SEARCH_FROM;

  useEffect(() => {
    function onClick(e: MouseEvent) {
      const target = e.target as Node;
      if (boxRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // Geometry lives in select-position.ts and is tested there; this only feeds
  // it the live rect and keeps it current while the page moves.
  useLayoutEffect(() => {
    if (!open) return;
    function place() {
      const rect = boxRef.current?.getBoundingClientRect();
      if (!rect) return;
      setMenu(
        placeMenu(rect, { width: window.innerWidth, height: window.innerHeight }),
      );
    }
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        o.value.toLowerCase().includes(q) ||
        (o.description?.toLowerCase().includes(q) ?? false),
    );
  }, [options, query]);

  useEffect(() => {
    if (active >= filtered.length) setActive(0);
  }, [filtered, active]);

  function pick(option: SelectOption) {
    if (option.disabled) return;
    onChange(option.value);
    setQuery("");
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(filtered.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter" && open && filtered[active]) {
      e.preventDefault();
      pick(filtered[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const selected = options.find((o) => o.value === value);
  let lastGroup: string | undefined;

  return (
    <div ref={boxRef} className={`relative ${className}`}>
      <button
        id={id}
        type="button"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onKeyDown}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-left text-sm disabled:cursor-not-allowed disabled:opacity-60"
        style={{
          background: "var(--surface-2)",
          border: `1px solid ${open ? "var(--accent)" : "var(--surface-border)"}`,
          color: "var(--ink-100)",
        }}
      >
        {icon}
        <span
          className={`min-w-0 flex-1 truncate ${mono ? "font-mono text-[13px]" : ""}`}
          style={selected ? undefined : { color: "var(--ink-muted)" }}
        >
          {selected?.label ?? placeholder}
        </span>
        {selected?.badge && <span className="pill !text-[9px] shrink-0">{selected.badge}</span>}
        <ChevronDown className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--ink-muted)" }} />
      </button>

      {open && !disabled && menu && typeof document !== "undefined" && createPortal(
        <div
          ref={menuRef}
          className="fixed z-[100] flex flex-col overflow-hidden rounded-lg"
          style={{
            left: menu.left,
            top: menu.top,
            bottom: menu.bottom,
            width: menu.width,
            maxHeight: menu.maxHeight,
            background: "var(--surface-raised)",
            border: "1px solid var(--border)",
            boxShadow: "var(--shadow-raised)",
          }}
        >
          {withSearch && options.length > 0 && (
            <div className="relative border-b" style={{ borderColor: "var(--border-subtle)" }}>
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5"
                style={{ color: "var(--ink-muted)" }}
              />
              <input
                autoFocus
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(0);
                }}
                onKeyDown={onKeyDown}
                placeholder="Search…"
                className="w-full bg-transparent py-2 pl-8 pr-2 text-sm outline-none"
                style={{ color: "var(--ink-strong)" }}
              />
            </div>
          )}
          <ul role="listbox" className="min-h-0 flex-1 overflow-y-auto py-1">
            {filtered.length === 0 && (
              <li className="px-3 py-3 text-sm" style={{ color: "var(--ink-muted)" }}>
                {options.length === 0 ? emptyLabel : `Nothing matches “${query}”.`}
              </li>
            )}
            {filtered.map((o, idx) => {
              const header = o.group && o.group !== lastGroup ? o.group : null;
              lastGroup = o.group;
              const isActive = idx === active;
              return (
                <li key={`${o.group ?? ""}:${o.value}`} role="option" aria-selected={o.value === value}>
                  {header && (
                    <div
                      className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider"
                      style={{ color: "var(--ink-muted)" }}
                    >
                      {header}
                    </div>
                  )}
                  <button
                    type="button"
                    disabled={o.disabled}
                    onMouseEnter={() => setActive(idx)}
                    onClick={() => pick(o)}
                    className="w-full px-3 py-1.5 text-left disabled:opacity-40"
                    style={{
                      background: isActive && !o.disabled ? "var(--accent-soft-bg-2)" : "transparent",
                      color: isActive && !o.disabled ? "var(--accent-soft-ink)" : "var(--ink)",
                    }}
                  >
                    <span className="flex items-center gap-2">
                      {o.value === value ? (
                        <Check className="w-3 h-3 shrink-0" style={{ color: "var(--accent)" }} />
                      ) : (
                        <span className="w-3 shrink-0" />
                      )}
                      <span className={`truncate ${mono ? "font-mono text-[13px]" : "text-sm"}`}>
                        {o.label}
                      </span>
                      {o.badge && <span className="pill !text-[9px] shrink-0">{o.badge}</span>}
                    </span>
                    {o.description && (
                      <span
                        className="ml-5 block truncate text-[11px]"
                        style={{ color: "var(--ink-muted)" }}
                      >
                        {o.description}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>,
        document.body,
      )}
    </div>
  );
}
