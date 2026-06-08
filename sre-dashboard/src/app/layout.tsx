import type { Metadata } from "next";
import "./globals.css";
import { THEME_INIT_SCRIPT } from "@/lib/theme-script";

export const metadata: Metadata = {
  title: "DevAI SRE — Cluster Monitoring",
  description: "AI-powered Site Reliability Engineering",
  icons: { icon: "/favicon.svg" },
};

/**
 * Root layout.
 *
 * Theme application (DASH-11): an inline blocking script (THEME_INIT_SCRIPT,
 * shared with next.config.ts for the CSP hash) reads the standardized
 * localStorage('devai-theme') key — the SAME key the ALM dashboard uses —
 * and `prefers-color-scheme` BEFORE React paints, then toggles `html.dark`.
 * This eliminates the dark/light flash on reload and replaces the old dead
 * ThemeProvider that wrote a different, never-read 'theme' key.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-screen bg-gray-50 dark:bg-gray-900" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
