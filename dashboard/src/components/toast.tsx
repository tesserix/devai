"use client";

/**
 * Reusable global toast notifications.
 *
 * Mount <ToastProvider> once (in the root layout) and call useToast() from any
 * client component:
 *
 *   const toast = useToast();
 *   toast.success("Created repo");
 *   toast.error("Something failed");
 *   const id = toast.loading("Creating…");  toast.dismiss(id);  // for in-progress actions
 *
 * Toasts auto-dismiss (errors linger longer); loading toasts stay until
 * dismissed or replaced via toast.update(id, …). Click a toast to dismiss it.
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";

export type ToastKind = "success" | "error" | "info" | "loading";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

export interface ToastApi {
  success: (message: string) => number;
  error: (message: string) => number;
  info: (message: string) => number;
  /** Persistent toast for an in-progress action; dismiss/update when it finishes. */
  loading: (message: string) => number;
  /** Replace a toast's kind + message (e.g. loading → success). */
  update: (id: number, kind: ToastKind, message: string) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TTL: Record<ToastKind, number> = {
  success: 4000,
  info: 4000,
  error: 7000,
  loading: 0, // sticky until dismissed/updated
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: number) => {
    const t = timers.current.get(id);
    if (t) {
      clearTimeout(t);
      timers.current.delete(id);
    }
    setToasts((prev) => prev.filter((x) => x.id !== id));
  }, []);

  const arm = useCallback(
    (id: number, kind: ToastKind) => {
      const ttl = TTL[kind];
      if (ttl > 0) timers.current.set(id, setTimeout(() => dismiss(id), ttl));
    },
    [dismiss]
  );

  const push = useCallback(
    (kind: ToastKind, message: string): number => {
      const id = ++seq.current;
      setToasts((prev) => [...prev, { id, kind, message }]);
      arm(id, kind);
      return id;
    },
    [arm]
  );

  const update = useCallback(
    (id: number, kind: ToastKind, message: string) => {
      const existing = timers.current.get(id);
      if (existing) {
        clearTimeout(existing);
        timers.current.delete(id);
      }
      setToasts((prev) => {
        if (!prev.some((x) => x.id === id)) return [...prev, { id, kind, message }];
        return prev.map((x) => (x.id === id ? { ...x, kind, message } : x));
      });
      arm(id, kind);
    },
    [arm]
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (m) => push("success", m),
      error: (m) => push("error", m),
      info: (m) => push("info", m),
      loading: (m) => push("loading", m),
      update,
      dismiss,
    }),
    [push, update, dismiss]
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="fixed bottom-4 right-4 z-[1000] flex w-full max-w-sm flex-col gap-2"
        aria-live="polite"
        role="region"
        aria-label="Notifications"
      >
        {toasts.map((t) => (
          <ToastRow key={t.id} toast={t} onClose={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastRow({ toast, onClose }: { toast: ToastItem; onClose: () => void }) {
  const style =
    toast.kind === "success"
      ? { background: "var(--ok-soft-bg)", color: "var(--ok-ink)", border: "1px solid var(--ok-soft-bd)" }
      : toast.kind === "error"
        ? {
            background: "var(--error-soft-bg)",
            color: "var(--error-ink)",
            border: "1px solid var(--error-soft-bd)",
          }
        : { background: "var(--surface-hover)", color: "var(--ink)", border: "1px solid var(--border-strong)" };

  const Icon =
    toast.kind === "success"
      ? CheckCircle2
      : toast.kind === "error"
        ? XCircle
        : toast.kind === "loading"
          ? Loader2
          : Info;

  return (
    <div
      role="status"
      onClick={onClose}
      className="flex cursor-pointer items-start gap-2 rounded-md px-3 py-2 text-sm shadow-lg transition-opacity"
      style={style}
    >
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${toast.kind === "loading" ? "animate-spin" : ""}`} />
      <span className="flex-1 leading-snug">{toast.message}</span>
      <X className="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-50" />
    </div>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>");
  return ctx;
}
