"use client";

import { useMemo, useRef, useState } from "react";

/**
 * Intent textarea with @-mention autocomplete — point the crew at exactly
 * what matters (files from the repo tree, team members). On "@", a popover
 * of suggestions appears; picking one inserts it as `@token`.
 *
 * The parent owns the value and the suggestion pool; this component just
 * handles the @-trigger + insertion, and reports the chosen mentions.
 */

export interface MentionSuggestion {
  /** The token inserted after @ (e.g. "drawer.tsx" or "alice"). */
  value: string;
  /** Optional secondary label shown in the list (e.g. "src/ui/drawer.tsx"). */
  hint?: string;
  /** What this mention is — drives the context_ref type on dispatch. */
  kind: "file" | "symbol" | "url" | "member";
}

export function MentionInput({
  value,
  onChange,
  suggestions,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  suggestions: MentionSuggestion[];
  placeholder?: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [query, setQuery] = useState<string | null>(null);

  const filtered = useMemo(() => {
    if (query === null) return [];
    const q = query.toLowerCase();
    return suggestions.filter((s) => s.value.toLowerCase().includes(q) || (s.hint || "").toLowerCase().includes(q)).slice(0, 8);
  }, [query, suggestions]);

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const text = e.target.value;
    onChange(text);
    // Detect an in-progress @token at the caret.
    const caret = e.target.selectionStart ?? text.length;
    const upToCaret = text.slice(0, caret);
    const match = /(^|\s)@([\w./-]*)$/.exec(upToCaret);
    setQuery(match ? match[2] : null);
  }

  function insert(s: MentionSuggestion) {
    const el = ref.current;
    const caret = el?.selectionStart ?? value.length;
    const before = value.slice(0, caret).replace(/@([\w./-]*)$/, "");
    const after = value.slice(caret);
    const next = `${before}@${s.value} ${after}`;
    onChange(next);
    setQuery(null);
    el?.focus();
  }

  return (
    <div className="relative">
      <textarea
        ref={ref}
        value={value}
        onChange={handleChange}
        placeholder={placeholder || "Describe the task… use @ to reference a file or teammate"}
        rows={4}
        className="w-full resize-y rounded-lg border px-3 py-2 text-sm outline-none"
        style={{
          background: "var(--surface)",
          borderColor: "var(--border-subtle)",
          color: "var(--text-strong)",
        }}
      />
      {filtered.length > 0 && (
        <ul
          className="absolute z-20 mt-1 max-h-56 w-72 overflow-auto rounded-lg border py-1 shadow-lg"
          style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
        >
          {filtered.map((s) => (
            <li key={`${s.kind}:${s.value}`}>
              <button
                type="button"
                onClick={() => insert(s)}
                className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm transition hover:opacity-80"
                style={{ color: "var(--text-strong)" }}
              >
                <span className="truncate">@{s.value}</span>
                <span className="shrink-0 text-[11px]" style={{ color: "var(--text-muted)" }}>
                  {s.hint || s.kind}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
