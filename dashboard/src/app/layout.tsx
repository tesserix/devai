import type { Metadata } from "next";
import { Inter, IBM_Plex_Mono, Source_Serif_4, Syne } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "./theme-provider";
import { MissionControlShell } from "@/components/mission-control-shell";

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
 * We default <html> to `dark` (matches the Fiber screenshot) and the
 * MissionControlNav reads the localStorage override on mount, so the
 * first paint is correct for repeat visitors without an SSR flash.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const fontVars = `${inter.variable} ${ibmPlexMono.variable} ${sourceSerif.variable} ${syne.variable}`;
  return (
    <html lang="en" className={`dark ${fontVars}`} suppressHydrationWarning>
      <body className="min-h-screen" suppressHydrationWarning>
        <ThemeProvider>
          <MissionControlShell>{children}</MissionControlShell>
        </ThemeProvider>
      </body>
    </html>
  );
}
