"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { FolderGit2, Loader2, Lock, Search } from "lucide-react";
import { api, type OnboardedRepo } from "@/lib/api";

/**
 * RepoPicker — the single, shared "pick a repo to act on" control.
 *
 * It lists ONLY repos that have been onboarded to DevAI (onboarding store,
 * state="onboarded" — i.e. the ones with `.platform/devai.yaml` merged on the
 * default branch). Onboarding is a deliberate, separate step on /Repos; every
 * surface that *uses* a repo (Compose, the Run dialog, ⌘K @) selects from this
 * onboarded set rather than from the whole org. If nothing is onboarded yet the
 * picker says so and links straight to /Repos.
 *
 * Why the onboarding store and not the marker-probe (/scm/repos?initialised=true
 * that the Board uses): the store is the authoritative, uncapped list of what's
 * been onboarded, it's an instant DB read, and it's exactly what /Repos produces
 * — the probe endpoint only checks the 30 most-recently-pushed repos, so an
 * onboarded-but-quiet repo could silently fall out of the list.
 */
export function RepoPicker({
  value,
  onChange,
  className,
  placeholder = "Search onboarded repos…",
  autoFocus = false,
}: {
  value: string;
  onChange: (fullName: string) => void;
  className?: string;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  const [repos, setRepos] = useState<OnboardedRepo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const boxRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await api.listOnboarded("onboarded");
      setRepos(Array.isArray(rows) ? rows : []);
    } catch {
      setRepos([]);
    } finally {
      setLoading(false);
      setLoaded(true);
    }
  }, []);

  // Load once the first time the dropdown opens (or eagerly when something is
  // already selected, so a deep-linked value resolves to its row).
  useEffect(() => {
    if ((open || value) && !loaded && !loading) void load();
  }, [open, value, loaded, loading, load]);

  // Close on outside click.
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter(
      (r) =>
        r.full_name.toLowerCase().includes(q) ||
        (r.description?.toLowerCase().includes(q) ?? false),
    );
  }, [repos, query]);

  useEffect(() => {
    if (active >= filtered.length) setActive(0);
  }, [filtered, active]);

  function select(fullName: string) {
    onChange(fullName);
    setQuery("");
    setOpen(false);
    // Warm the repo profile (tech stack, frameworks) so the run launched right
    // after has context. Fire-and-forget — the run never blocks on the scan.
    const [owner, name] = fullName.split("/", 2);
    if (owner && name) {
      fetch(
        `/api/scm/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/scan`,
        { method: "POST" },
      ).catch(() => undefined);
    }
  }

  const inputStyle = {
    background: "var(--surface)",
    border: "1px solid var(--border-subtle)",
    color: "var(--ink-strong)",
  } as const;

  return (
    <div ref={boxRef} className={`relative ${className ?? ""}`}>
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5"
          style={{ color: "var(--ink-muted)" }}
        />
        <input
          value={open ? query : value}
          autoFocus={autoFocus}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
            if (!open) setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setActive((i) => Math.min(filtered.length - 1, i + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => Math.max(0, i - 1));
            } else if (e.key === "Enter" && open && filtered[active]) {
              e.preventDefault();
              select(filtered[active].full_name);
            } else if (e.key === "Escape") {
              setOpen(false);
            }
          }}
          placeholder={value ? value : placeholder}
          className="w-full rounded-md py-1.5 pl-7 pr-2 text-sm outline-none focus:border-[var(--ink-strong)]"
          style={inputStyle}
        />
      </div>

      {open && (
        <div
          className="absolute z-20 mt-1 w-full overflow-hidden rounded-md"
          style={{
            background: "var(--surface-raised)",
            border: "1px solid var(--border)",
            boxShadow: "var(--shadow-raised)",
          }}
        >
          <div className="max-h-60 overflow-y-auto py-1">
            {loading && repos.length === 0 ? (
              <div
                className="flex items-center gap-2 px-3 py-3 text-sm"
                style={{ color: "var(--ink-muted)" }}
              >
                <Loader2 className="w-4 h-4 animate-spin" /> Loading onboarded repos…
              </div>
            ) : repos.length === 0 ? (
              <div className="px-3 py-4 text-sm" style={{ color: "var(--ink-muted)" }}>
                <p className="mb-2">No repos onboarded to DevAI yet.</p>
                <Link
                  href="/repos"
                  className="inline-flex items-center gap-1.5 font-medium"
                  style={{ color: "var(--accent)" }}
                  onClick={() => setOpen(false)}
                >
                  <FolderGit2 className="w-3.5 h-3.5" /> Onboard a repo in Repos →
                </Link>
              </div>
            ) : filtered.length === 0 ? (
              <div className="px-3 py-3 text-sm" style={{ color: "var(--ink-muted)" }}>
                No onboarded repo matches “{query}”.
              </div>
            ) : (
              <ul>
                {filtered.map((r, idx) => {
                  const isActive = idx === active;
                  return (
                    <li key={r.full_name}>
                      <button
                        type="button"
                        onMouseEnter={() => setActive(idx)}
                        onClick={() => select(r.full_name)}
                        className="w-full px-3 py-2 text-left transition-colors"
                        style={{
                          background: isActive ? "var(--accent-soft-bg-2)" : "transparent",
                          color: isActive ? "var(--accent-soft-ink)" : "var(--ink)",
                        }}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex items-center gap-1.5 truncate font-mono text-sm">
                            <FolderGit2
                              className="w-3.5 h-3.5 shrink-0"
                              style={{ color: isActive ? "var(--accent)" : "var(--ink-muted)" }}
                            />
                            {r.full_name}
                          </span>
                          {r.tags?.includes("private") && (
                            <Lock className="w-3 h-3 shrink-0" style={{ color: "var(--ink-muted)" }} />
                          )}
                        </div>
                        {r.description && (
                          <p
                            className="mt-0.5 truncate pl-5 text-xs"
                            style={{ color: "var(--ink-soft)" }}
                          >
                            {r.description}
                          </p>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          {repos.length > 0 && (
            <div
              className="border-t px-3 py-1.5 text-[11px]"
              style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
            >
              Need another repo?{" "}
              <Link
                href="/repos"
                className="font-medium hover:underline"
                style={{ color: "var(--accent)" }}
                onClick={() => setOpen(false)}
              >
                Onboard it in Repos
              </Link>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
