"use client";

import { useEffect } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";

/**
 * Route-level error boundary. Catches any client-side exception thrown while
 * rendering a page in this segment and shows a recoverable error card instead
 * of white-screening the whole app ("Application error: a client-side
 * exception has occurred"). `reset()` re-renders the segment.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface to the console for debugging; a real telemetry sink can hook here.
    console.error("Dashboard route error:", error);
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] p-8 text-center">
      <TriangleAlert className="w-10 h-10 text-amber-400 mb-4" />
      <h1 className="font-serif text-xl font-medium text-[var(--ink-50)]">Something went wrong</h1>
      <p className="text-sm text-[var(--ink-300)] mt-2 max-w-md">
        This page hit an unexpected error and couldn&apos;t render. The rest of the dashboard is
        unaffected — you can retry or navigate elsewhere.
      </p>
      {error?.message && (
        <pre className="mt-4 text-xs text-[var(--ink-400)] bg-[var(--surface-raised)] rounded-md px-3 py-2 max-w-lg overflow-x-auto">
          {error.message}
          {error.digest ? `\n(digest: ${error.digest})` : ""}
        </pre>
      )}
      <button
        onClick={reset}
        className="btn-primary text-sm flex items-center gap-1.5 mt-5"
      >
        <RefreshCw className="w-3.5 h-3.5" /> Try again
      </button>
    </div>
  );
}
