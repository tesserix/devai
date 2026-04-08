import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DevAI — ALM Pipeline Dashboard",
  description: "AI-powered Application Lifecycle Management",
  icons: { icon: "/favicon.svg" },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className="min-h-screen bg-[var(--bg-primary)]" suppressHydrationWarning>{children}</body>
    </html>
  );
}
