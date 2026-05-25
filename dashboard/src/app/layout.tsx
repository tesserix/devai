import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "./theme-provider";
import { MissionControlShell } from "@/components/mission-control-shell";

export const metadata: Metadata = {
  title: "DevAI — Mission Control",
  description: "AI-powered Application Lifecycle Management",
  icons: { icon: "/favicon.svg" },
};

/**
 * Root layout — Fiber-style mission-control shell: left sidebar + main
 * content. The login page renders its own minimal full-width frame; the
 * shell component pass-throughs when the current path is /login.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-gray-50 dark:bg-gray-900" suppressHydrationWarning>
        <ThemeProvider>
          <MissionControlShell>{children}</MissionControlShell>
        </ThemeProvider>
      </body>
    </html>
  );
}
