"use client";

import { useEffect, useState } from "react";

/**
 * Bottom status bar — mirrors the Fiber screenshot's "1 active · 0
 * complete · 0 failed · 0 PRs · $0.00 today" footer plus an active-
 * page indicator on the right.
 *
 * Pulls real counts when /api/registry and /api/pipeline are
 * reachable. Falls back to zeros so the bar still renders during
 * cold-start or when the backend is offline. We poll every 30s
 * (matching the registry cache TTL) — the cost is one tiny GET.
 */
type Counts = { active: number; complete: number; failed: number; prs: number };

export function StatusBar() {
  const [counts, setCounts] = useState<Counts>({ active: 0, complete: 0, failed: 0, prs: 0 });
  const [registry, setRegistry] = useState<number | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      const next: Counts = { active: 0, complete: 0, failed: 0, prs: 0 };
      try {
        const r = await fetch("/api/pipeline/runs?limit=200", { cache: "no-store" });
        if (r.ok) {
          const runs = (await r.json()) as Array<{ status?: string; state?: string }>;
          for (const run of runs) {
            const state = (run.status || run.state || "").toLowerCase();
            if (state.includes("running") || state === "active" || state === "pending") next.active += 1;
            else if (state.includes("complet") || state === "merged") next.complete += 1;
            else if (state.includes("fail")) next.failed += 1;
          }
        }
      } catch {
        // ignore — surface as 0s
      }
      try {
        const r = await fetch("/api/registry/counts", { cache: "no-store" });
        if (r.ok) {
          const c = (await r.json()) as { skills?: number; agents?: number; prompts?: number; mcp_servers?: number };
          setRegistry((c.skills ?? 0) + (c.agents ?? 0) + (c.prompts ?? 0) + (c.mcp_servers ?? 0));
          setConnected(true);
        } else {
          setConnected(false);
        }
      } catch {
        setConnected(false);
      }
      if (!cancelled) setCounts(next);
    }

    tick();
    const id = window.setInterval(tick, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <footer className="shrink-0 h-9 px-4 border-t border-[var(--surface-border)] bg-[var(--surface-1)]/80 backdrop-blur-sm flex items-center justify-between text-[11px] text-[var(--ink-300)]">
      <div className="flex items-center gap-5 font-mono">
        <Stat label="active" value={counts.active} dot="dot-running" />
        <Stat label="complete" value={counts.complete} dot="dot-ok" />
        <Stat label="failed" value={counts.failed} dot="dot-error" />
        <Stat label="PRs" value={counts.prs} dot="dot-muted" />
        {registry !== null && (
          <Stat label="catalog" value={registry} dot="dot-muted" />
        )}
      </div>
      <div className="flex items-center gap-2">
        <span className={`dot ${connected ? "dot-ok" : connected === false ? "dot-error" : "dot-muted"}`} />
        <span className="font-mono">
          {connected ? "connected" : connected === false ? "offline" : "…"}
        </span>
      </div>
    </footer>
  );
}

function Stat({ label, value, dot }: { label: string; value: number; dot: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`dot ${dot}`} />
      <span className="text-[var(--ink-100)] tabular-nums">{value}</span>
      <span className="text-[var(--ink-500)]">{label}</span>
    </span>
  );
}
