"use client";

import { usePathname, useRouter } from "next/navigation";
import { useCallback } from "react";
import { MissionControlNav } from "@/components/mission-control-nav";
import { StatusBar } from "@/components/status-bar";
import { CommandPalette } from "@/components/command-palette";

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

  const isAuth = pathname.startsWith("/login");

  const toggleDark = useCallback(() => {
    const html = document.documentElement;
    const wasDark = html.classList.contains("dark");
    const next = !wasDark;
    html.classList.toggle("dark", next);
    window.localStorage.setItem("devai-theme", next ? "dark" : "light");
  }, []);

  const onNewTask = useCallback(() => router.push("/control?action=new"), [router]);

  if (isAuth) {
    return <>{children}</>;
  }

  return (
    <div className="flex min-h-screen flex-col">
      <div className="flex flex-1 min-h-0">
        <MissionControlNav />
        <main className="flex-1 min-w-0 overflow-y-auto">{children}</main>
      </div>
      <StatusBar />
      <CommandPalette toggleDark={toggleDark} onNewTask={onNewTask} />
    </div>
  );
}
