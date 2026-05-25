"use client";

import { usePathname } from "next/navigation";
import { MissionControlNav } from "@/components/mission-control-nav";

/**
 * Renders the left-nav shell for application pages, and a bare
 * pass-through frame for auth pages (currently just /login). Keeping
 * the logic on the client lets the nav highlight stay reactive
 * without a per-page server `if`.
 */
export function MissionControlShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isAuth = pathname.startsWith("/login");
  if (isAuth) {
    return <>{children}</>;
  }
  return (
    <div className="flex min-h-screen">
      <MissionControlNav />
      <main className="flex-1 min-w-0">{children}</main>
    </div>
  );
}
