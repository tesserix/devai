"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { signInWithGoogle, exchangeForSession } from "@/lib/firebase";
import { safeReturn } from "@/lib/safe-return";

// useSearchParams() forces this page out of the static-prerender path, so
// we wrap the search-params consumer in a Suspense boundary as required
// by Next 15's CSR-bailout contract.
export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginPageInner />
    </Suspense>
  );
}

function LoginPageInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // null = still detecting; "local_db" = username/password; otherwise GIP.
  const [mode, setMode] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // Where to send the browser after a successful sign-in. The ?return_to=…
  // param is honoured ONLY after validation by safeReturn() — an unvalidated
  // return_to is a classic post-auth open-redirect / phishing primitive
  // (DASH-1). We accept a same-site relative path, or an absolute URL whose
  // host is tesserix.app / *.tesserix.app (the session cookie is on
  // .tesserix.app so those are legitimately in-family). Anything else → "/".
  const returnTo = safeReturn(params.get("return_to"));

  // Ask the backend which auth mode is active. local_db → password form;
  // anything else (or a failure) → the Google/GIP button.
  useEffect(() => {
    fetch("/auth/config", { credentials: "include", cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => setMode(cfg?.mode === "local_db" ? "local_db" : "gip"))
      .catch(() => setMode("gip"));
  }, []);

  function goHome() {
    // returnTo is already validated by safeReturn(); an absolute value here is
    // guaranteed to be on the tesserix.app family, a relative one is same-site.
    if (returnTo.startsWith("http")) window.location.href = returnTo;
    else router.replace(returnTo);
  }

  async function handleGoogleSignIn() {
    setError(null);
    setPending(true);
    try {
      const { idToken, pool, tenantId } = await signInWithGoogle();
      await exchangeForSession(idToken, pool, tenantId);
      goHome();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
      setPending(false);
    }
  }

  async function handleLocalLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Login failed (${res.status})`);
      }
      goHome();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
      setPending(false);
    }
  }

  const inputCls =
    "w-full px-3 py-2.5 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-sm placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-indigo-500";

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-indigo-600 mb-5">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </div>
          <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100 tracking-tight">
            DevAI Platform
          </h1>
          <p className="mt-1.5 text-sm text-gray-500 dark:text-gray-400">
            AI-powered Application Lifecycle Management
          </p>
        </div>

        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-7 shadow-sm">
          <h2 className="text-sm font-medium text-gray-700 dark:text-gray-300 text-center mb-5">
            Sign in to continue
          </h2>

          {mode === "local_db" ? (
            <form onSubmit={handleLocalLogin} className="space-y-3">
              <input
                className={inputCls}
                type="text"
                placeholder="Username (admin / dev / qa / designer)"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                autoFocus
              />
              <input
                className={inputCls}
                type="password"
                placeholder="Password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <button
                type="submit"
                disabled={pending}
                className="w-full flex items-center justify-center px-4 py-2.5 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 transition-colors shadow-sm cursor-pointer disabled:opacity-50 disabled:cursor-wait"
              >
                {pending ? "Signing in…" : "Sign in"}
              </button>
            </form>
          ) : (
            <button
              onClick={handleGoogleSignIn}
              disabled={pending || mode === null}
              className="w-full flex items-center justify-center gap-3 px-4 py-2.5 rounded-lg border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors shadow-sm cursor-pointer disabled:opacity-50 disabled:cursor-wait"
            >
              <svg width="16" height="16" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 01-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z" fill="#4285F4"/>
                <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 009 18z" fill="#34A853"/>
                <path d="M3.964 10.71A5.41 5.41 0 013.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.997 8.997 0 000 9c0 1.452.348 2.827.957 4.042l3.007-2.332z" fill="#FBBC05"/>
                <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 00.957 4.958L3.964 6.29C4.672 4.163 6.656 2.58 9 3.58z" fill="#EA4335"/>
              </svg>
              {pending ? "Signing in…" : "Sign in with Google"}
            </button>
          )}

          {error && (
            <p className="mt-4 text-xs text-red-600 dark:text-red-400 text-center">{error}</p>
          )}

          <p className="mt-5 text-center text-xs text-gray-400 dark:text-gray-500">
            Access restricted to authorized users
          </p>
        </div>

        <p className="mt-6 text-center text-xs text-gray-400 dark:text-gray-500">
          Tesserix Platform
        </p>
      </div>
    </div>
  );
}
