import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DevAI — ALM Pipeline Dashboard",
  description: "AI-powered Application Lifecycle Management",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[var(--bg-primary)]">{children}</body>
    </html>
  );
}
