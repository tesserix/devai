"use client";

import { useEffect } from "react";

/**
 * Global error boundary — the last line of defense. Catches exceptions thrown
 * in the root layout itself (which the segment-level error.tsx cannot). Must
 * render its own <html>/<body> because it replaces the root layout when it
 * fires. Without this, a layout-level throw shows Next.js's bare
 * "Application error" white screen.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Dashboard global error:", error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "system-ui, sans-serif",
          background: "#0b0b0f",
          color: "#e6e6ea",
          padding: "2rem",
          textAlign: "center",
        }}
      >
        <h1 style={{ fontSize: "1.25rem", fontWeight: 500 }}>The dashboard failed to load</h1>
        <p style={{ fontSize: "0.875rem", opacity: 0.7, maxWidth: 480, marginTop: "0.5rem" }}>
          An unexpected error occurred while loading the application. Please retry.
        </p>
        <button
          onClick={reset}
          style={{
            marginTop: "1.25rem",
            padding: "0.5rem 1rem",
            borderRadius: 8,
            border: "1px solid #3a3a45",
            background: "#1a1a22",
            color: "#e6e6ea",
            cursor: "pointer",
            fontSize: "0.875rem",
          }}
        >
          Try again
        </button>
      </body>
    </html>
  );
}
