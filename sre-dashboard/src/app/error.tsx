"use client";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex h-screen items-center justify-center bg-gray-50 dark:bg-gray-900">
      <div className="text-center max-w-md px-6">
        <div className="w-12 h-12 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/20 flex items-center justify-center mx-auto mb-4">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-red-500 dark:text-red-400">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
        </div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Something went wrong
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          {error.message || "An unexpected error occurred"}
        </p>
        <button
          onClick={reset}
          className="px-4 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 transition-colors"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
