"use client";

/**
 * Free-allowance surface: an onboarding panel of things to try, a
 * persistent remaining-tokens meter, an >=80% warning, and an exhaustion
 * prompt pointing at Settings.
 *
 * All state comes from GET /api/settings/trial, via trialTone — never
 * re-derive. soft:true means a failed fetch (401 included) just renders
 * nothing rather than bouncing a signed-in user off the page they're on.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { api, type TrialStatus } from "@/lib/api";
import { trialTone } from "@/lib/admin-api";
import { DEMO_IDEAS, shouldShowOnboarding } from "@/lib/demo-ideas";

const SEEN_KEY = "devai-trial-onboarding-seen";

export function TrialBanner() {
  const pathname = usePathname();
  const [status, setStatus] = useState<TrialStatus | null>(null);
  const [seen, setSeen] = useState(true);

  useEffect(() => {
    setSeen(window.localStorage.getItem(SEEN_KEY) === "1");
    api
      .getTrialStatus()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  // Settings already carries its own trial notice, and the exhaustion prompt links there.
  if (pathname === "/settings") return null;

  const tone = trialTone(status);
  if (tone === "hidden" || !status) return null;

  const dismiss = () => {
    window.localStorage.setItem(SEEN_KEY, "1");
    setSeen(true);
  };

  if (tone === "ok" && shouldShowOnboarding(seen, status)) {
    return (
      <div className="panel mb-4" style={{ padding: 16 }}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-display text-base" style={{ color: "var(--ink-strong)" }}>
              You have {status.remaining.toLocaleString()} free tokens
            </h3>
            <p className="mt-1 text-sm" style={{ color: "var(--ink-muted)" }}>
              Runs on the platform&apos;s own model providers. A few things worth trying.
            </p>
          </div>
          <button onClick={dismiss} className="text-xs underline" style={{ color: "var(--ink-muted)" }}>
            Dismiss
          </button>
        </div>
        <ul className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {DEMO_IDEAS.map((idea) => (
            <li key={idea.href} className="rounded" style={{ border: "1px solid var(--border)", padding: 12 }}>
              <Link href={idea.href} className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                {idea.title}
              </Link>
              <p className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                {idea.blurb}
              </p>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (tone === "exhausted") {
    return (
      <div className="panel mb-4" style={{ padding: 16 }}>
        <h3 className="font-display text-base" style={{ color: "var(--ink-strong)" }}>
          Your free tokens are used up
        </h3>
        <p className="mt-1 text-sm" style={{ color: "var(--ink-muted)" }}>
          Add your own provider key to keep running.{" "}
          <Link href="/settings" className="underline">
            Go to Settings
          </Link>
        </p>
      </div>
    );
  }

  const pct = status.budget > 0 ? Math.min(100, Math.round((status.used / status.budget) * 100)) : 0;

  return (
    <div className="panel mb-4 flex items-center gap-3" style={{ padding: "8px 16px", fontSize: 12 }}>
      <span style={{ color: "var(--ink-muted)" }}>Free allowance</span>
      <span className="h-1.5 w-32 overflow-hidden rounded" style={{ background: "var(--border)" }}>
        <span className="block h-full" style={{ width: `${pct}%`, background: "var(--accent)" }} />
      </span>
      <span style={{ color: "var(--ink-strong)" }}>{status.remaining.toLocaleString()} left</span>
      {tone === "warning" && (
        <Link href="/settings" className="ml-auto underline" style={{ color: "var(--ink-muted)" }}>
          Add own key
        </Link>
      )}
    </div>
  );
}
