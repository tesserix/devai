"use client";

// A small, local toast for the SRE dashboard (DASH-11). The SRE dashboard is a
// separate app and must not import the ALM dashboard's ToastProvider, so this
// is a deliberately minimal implementation in the same visual language
// (gray/emerald surfaces, rounded-lg, subtle border + shadow).
//
// Usage:
//   const toast = useToast();
//   toast.error("Could not trigger scan: …");
//   toast.success("Scan triggered");
//
// Mount <ToastViewport/> once near the dashboard root.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";
import { clsx } from "clsx";

type ToastKind = "success" | "error" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastCtx = createContext<{
  items: ToastItem[];
  push: (kind: ToastKind, message: string) => void;
  dismiss: (id: number) => void;
} | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++;
      setItems((prev) => [...prev, { id, kind, message }]);
      // Auto-dismiss; errors linger longer so they can be read.
      window.setTimeout(() => dismiss(id), kind === "error" ? 7000 : 4000);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ items, push, dismiss }), [items, push, dismiss]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <ToastViewport />
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  // Degrade gracefully if used outside a provider (e.g. in isolation).
  if (!ctx) {
    const noop = () => {};
    return { success: noop, error: noop, info: noop };
  }
  return {
    success: (m) => ctx.push("success", m),
    error: (m) => ctx.push("error", m),
    info: (m) => ctx.push("info", m),
  };
}

function ToastViewport() {
  const ctx = useContext(ToastCtx);
  if (!ctx || ctx.items.length === 0) return null;

  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm"
      aria-live="polite"
      role="status"
    >
      {ctx.items.map((t) => (
        <div
          key={t.id}
          className={clsx(
            "flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 shadow-sm text-sm",
            "sre-toast-in",
            t.kind === "error" &&
              "border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-300",
            t.kind === "success" &&
              "border-emerald-200 dark:border-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
            t.kind === "info" &&
              "border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200",
          )}
        >
          <span aria-hidden className="mt-0.5 shrink-0">
            {t.kind === "error" ? "⚠" : t.kind === "success" ? "✓" : "ℹ"}
          </span>
          <span className="flex-1 break-words">{t.message}</span>
          <button
            type="button"
            onClick={() => ctx.dismiss(t.id)}
            className="shrink-0 -mr-1 px-1 opacity-60 hover:opacity-100 transition-opacity"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
