import type { Metadata } from "next";
import { Inter, IBM_Plex_Mono, Source_Serif_4, Syne } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "./theme-provider";
import { MissionControlShell } from "@/components/mission-control-shell";
import { ToastProvider } from "@/components/toast";

// Fonts mirror PLATFORM.md §14 (Tactical Monospace). We load only the
// weights we actually use to keep the bundle lean.
const inter = Inter({ subsets: ["latin"], display: "swap", variable: "--font-sans-loaded", weight: ["400", "500", "600", "700"] });
const ibmPlexMono = IBM_Plex_Mono({ subsets: ["latin"], display: "swap", variable: "--font-mono-loaded", weight: ["400", "500", "600"] });
const sourceSerif = Source_Serif_4({ subsets: ["latin"], display: "swap", variable: "--font-serif-loaded", weight: ["400", "500", "600"] });
const syne = Syne({ subsets: ["latin"], display: "swap", variable: "--font-display-loaded", weight: ["500", "600", "700"] });

export const metadata: Metadata = {
  title: "DevAI — Mission Control",
  description: "AI-powered Application Lifecycle Management",
  icons: { icon: "/favicon.svg" },
};

/**
 * Root layout — Fiber-style mission-control shell. The login page
 * renders its own minimal full-width frame; the shell pass-throughs
 * when the current path is /login.
 *
 * Theme application:
 *   1. An inline blocking script reads localStorage('devai-theme') and
 *      `prefers-color-scheme` BEFORE React paints, then toggles
 *      `html.dark`. This eliminates the dark/light flash on reload
 *      that every theme-toggle without SSR cookies suffers from.
 *   2. The Settings page persists the choice into localStorage. The
 *      sidebar toggle is a thin wrapper over the same key.
 *   3. New visitors get the system preference; explicit toggles win.
 */
const THEME_INIT_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem('devai-theme');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var useDark = stored ? stored === 'dark' : prefersDark;
    if (useDark) document.documentElement.classList.add('dark');
  } catch (e) {}
})();
`.trim();

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const fontVars = `${inter.variable} ${ibmPlexMono.variable} ${sourceSerif.variable} ${syne.variable}`;
  return (
    <html lang="en" className={fontVars} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-screen" suppressHydrationWarning>
        <ThemeProvider>
          <ToastProvider>
            <MissionControlShell>{children}</MissionControlShell>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
