"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { signInWithGoogle, exchangeForSession } from "@/lib/firebase";
import { safeReturn } from "@/lib/safe-return";

// useSearchParams() forces this page out of the static-prerender path,
// so we wrap the search-params consumer in a Suspense boundary as
// required by Next 15's CSR-bailout contract.
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

  // Where to send the browser after a successful sign-in. The raw
  // ?return_to=… param is UNTRUSTED — it is run through safeReturn() which
  // allows only same-site relative paths or *.tesserix.app absolute URLs
  // and otherwise falls back to "/". This closes the open-redirect /
  // post-auth phishing primitive (DASH-1). The session cookie is on
  // .tesserix.app so in-family absolute returns travel correctly.
  const returnTo = safeReturn(params.get("return_to"));

  async function handleGoogleSignIn() {
    setError(null);
    setPending(true);
    try {
      const { idToken, pool, tenantId } = await signInWithGoogle();
      await exchangeForSession(idToken, pool, tenantId);
      // safeReturn already validated the destination; an absolute URL is
      // necessarily within the tesserix.app family, a relative path is
      // same-site. Absolute → full navigation, relative → SPA replace.
      if (/^https?:\/\//.test(returnTo)) {
        window.location.href = returnTo;
      } else {
        router.replace(returnTo);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Sign-in failed";
      setError(msg);
      setPending(false);
    }
  }

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
            DevAI SRE
          </h1>
          <p className="mt-1.5 text-sm text-gray-500 dark:text-gray-400">
            Production SRE Monitoring
          </p>
        </div>

        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-7 shadow-sm">
          <h2 className="text-sm font-medium text-gray-700 dark:text-gray-300 text-center mb-5">
            Sign in to continue
          </h2>

          <button
            onClick={handleGoogleSignIn}
            disabled={pending}
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
