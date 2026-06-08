"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertTriangle, HelpCircle } from "lucide-react";

/**
 * App-wide confirmation dialog — a single, nicer, themed replacement for the
 * native window.confirm(). Promise-based so any action can gate on it:
 *
 *   const confirm = useConfirm();
 *   if (await confirm({ title: "Delete run?", message: "…", tone: "danger" })) {
 *     // do it
 *   }
 *
 * Mount <ConfirmProvider> once (in the root layout). One dialog instance is
 * reused for every call, so there's no per-call DOM cost and styling stays
 * consistent everywhere. Esc / backdrop / Cancel resolve false; Enter / the
 * confirm button resolve true.
 */

type ConfirmTone = "default" | "danger";

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: ConfirmTone;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within <ConfirmProvider>");
  return ctx;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);
  const confirmBtnRef = useRef<HTMLButtonElement | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (o) =>
      new Promise<boolean>((resolve) => {
        resolver.current = resolve;
        setOpts(o);
      }),
    [],
  );

  const settle = useCallback((value: boolean) => {
    setOpts(null);
    resolver.current?.(value);
    resolver.current = null;
  }, []);

  // Focus the confirm button when the dialog opens.
  useEffect(() => {
    if (opts) {
      const t = setTimeout(() => confirmBtnRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [opts]);

  // Esc cancels, Enter confirms — global while open.
  useEffect(() => {
    if (!opts) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        settle(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        settle(true);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [opts, settle]);

  const danger = opts?.tone === "danger";

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {opts && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center p-4"
          style={{ background: "var(--surface-overlay)", backdropFilter: "blur(4px)" }}
          onClick={(e) => e.target === e.currentTarget && settle(false)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-title"
        >
          <div
            className="w-full max-w-sm rounded-xl p-5"
            style={{
              background: "var(--surface-raised)",
              border: "1px solid var(--border)",
              boxShadow: "var(--shadow-raised)",
              color: "var(--ink)",
            }}
          >
            <div className="flex items-start gap-3">
              <span
                className="mt-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
                style={{
                  background: danger ? "var(--error-soft-bg)" : "var(--accent-soft-bg)",
                  color: danger ? "var(--error)" : "var(--accent)",
                }}
              >
                {danger ? <AlertTriangle className="h-4.5 w-4.5" /> : <HelpCircle className="h-4.5 w-4.5" />}
              </span>
              <div className="min-w-0 flex-1">
                <h2
                  id="confirm-title"
                  className="font-serif text-base font-medium"
                  style={{ color: "var(--ink-strong)" }}
                >
                  {opts.title}
                </h2>
                {opts.message && (
                  <div className="mt-1 text-sm leading-relaxed" style={{ color: "var(--ink-soft)" }}>
                    {opts.message}
                  </div>
                )}
              </div>
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => settle(false)}
                className="rounded-md px-3.5 py-2 text-sm font-medium transition-colors"
                style={{
                  color: "var(--ink)",
                  background: "var(--surface)",
                  border: "1px solid var(--border)",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface)")}
              >
                {opts.cancelLabel || "Cancel"}
              </button>
              <button
                ref={confirmBtnRef}
                type="button"
                onClick={() => settle(true)}
                className="rounded-md px-3.5 py-2 text-sm font-medium transition-opacity hover:opacity-90 focus:outline-none focus:ring-2"
                style={{
                  background: danger ? "var(--error)" : "var(--primary)",
                  color: danger ? "#fff" : "var(--primary-ink)",
                }}
              >
                {opts.confirmLabel || "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
