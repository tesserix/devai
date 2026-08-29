"use client";

import { usePathname, useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { Menu, ShieldHalf } from "lucide-react";
import { MissionControlNav } from "@/components/mission-control-nav";
import { StatusBar } from "@/components/status-bar";
import { CommandPalette } from "@/components/command-palette";
import { TrialBanner } from "@/components/trial-banner";
import { NEW_RUN_HREF } from "@/lib/run-entry";

/**
 * Top-level shell: pass-through for /login (no nav), mission-control
 * frame for everything else. The frame is:
 *
 *   ┌─────────┬──────────────────────────────────────┐
 *   │  nav    │        main (page content)           │
 *   │         │                                      │
 *   │         │                                      │
 *   │         ├──────────────────────────────────────┤
 *   │         │  status bar (counts + connection)    │
 *   └─────────┴──────────────────────────────────────┘
 *
 * The CommandPalette overlays on ⌘K and dismisses to nothing — zero
 * DOM cost when closed.
 */
export function MissionControlShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // Mobile nav drawer (md+ shows the rail inline, so this is a no-op there).
  const [navOpen, setNavOpen] = useState(false);

  const isAuth = pathname.startsWith("/login");

  const toggleDark = useCallback(() => {
    const html = document.documentElement;
    const wasDark = html.classList.contains("dark");
    const next = !wasDark;
    html.classList.toggle("dark", next);
    window.localStorage.setItem("devai-theme", next ? "dark" : "light");
  }, []);

  // The "New task" action opens the lifecycle home (Workflows), where the
  // blueprint picker + trigger dialog live — NOT the removed /control stub (DASH-9).
  const onNewTask = useCallback(() => router.push(NEW_RUN_HREF), [router]);

  if (isAuth) {
    return <>{children}</>;
  }

  return (
    <div className="flex min-h-screen flex-col">
      <div className="flex flex-1 min-h-0">
        {/* Backdrop behind the mobile drawer. */}
        {navOpen && (
          <div
            className="fixed inset-0 z-30 md:hidden"
            style={{ background: "rgba(10, 11, 14, 0.45)", backdropFilter: "blur(2px)" }}
            onClick={() => setNavOpen(false)}
            aria-hidden
          />
        )}
        <MissionControlNav open={navOpen} onClose={() => setNavOpen(false)} />
        <div className="flex-1 min-w-0 flex flex-col">
          {/* Mobile-only top bar with the hamburger (the rail is the nav on md+). */}
          <header
            className="md:hidden sticky top-0 z-20 h-14 flex items-center gap-2.5 px-3 border-b backdrop-blur"
            style={{
              borderColor: "var(--border-subtle)",
              background: "color-mix(in srgb, var(--surface) 85%, transparent)",
            }}
          >
            <button
              type="button"
              onClick={() => setNavOpen(true)}
              className="btn-ghost p-2 -ml-1"
              aria-label="Open menu"
            >
              <Menu className="w-5 h-5" />
            </button>
            <span className="inline-flex items-center gap-2">
              <span
                className="inline-flex w-7 h-7 rounded-md items-center justify-center shadow-sm"
                style={{ background: "var(--primary)", color: "var(--primary-ink)" }}
              >
                <ShieldHalf className="w-4 h-4" />
              </span>
              <span className="font-serif text-[15px]" style={{ color: "var(--ink-strong)" }}>
                DevAI
              </span>
            </span>
          </header>
          <main className="flex-1 min-w-0 overflow-y-auto">
            <TrialBanner />
            {children}
          </main>
        </div>
      </div>
      <StatusBar />
      <CommandPalette toggleDark={toggleDark} onNewTask={onNewTask} />
    </div>
  );
}
