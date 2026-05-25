// Firebase Auth (GIP) client for DevAI dashboards.
//
// Runtime config: the dashboard fetches its Firebase apiKey / authDomain /
// projectId / tenantId from `GET /auth/config` on the BFF rather than
// reading process.env. This keeps every credential — even non-secret
// public identifiers — in GCP Secret Manager and out of the git tree.
//
// The BFF picks the tenant pool based on the inbound Host (alm for
// devai/kagent/aregistry, sre for sre), so the same dashboard bundle
// adapts at runtime.

"use client";

import { initializeApp, getApps, getApp, type FirebaseApp } from "firebase/app";
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  type Auth,
} from "firebase/auth";

interface AuthConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  tenantId: string;
  pool: string;
}

let configPromise: Promise<AuthConfig> | null = null;
let app: FirebaseApp | null = null;
let authInstance: Auth | null = null;

async function fetchConfig(): Promise<AuthConfig> {
  if (!configPromise) {
    configPromise = fetch("/auth/config", {
      credentials: "include",
      cache: "no-store",
    }).then(async (res) => {
      if (!res.ok) {
        throw new Error(`auth/config returned ${res.status}`);
      }
      return (await res.json()) as AuthConfig;
    });
  }
  return configPromise;
}

async function getFirebaseAuth(): Promise<{ auth: Auth; config: AuthConfig }> {
  const config = await fetchConfig();
  if (!authInstance) {
    app = getApps().length
      ? getApp()
      : initializeApp({
          apiKey: config.apiKey,
          authDomain: config.authDomain,
          projectId: config.projectId,
        });
    authInstance = getAuth(app);
  }
  // Tenant binding refreshes every call — cheap, and tolerates the BFF
  // changing the tenant a hostname maps onto (e.g. agentic pool joins
  // alm vs sre realignment).
  if (config.tenantId) {
    authInstance.tenantId = config.tenantId;
  }
  return { auth: authInstance, config };
}

export interface SignInResult {
  idToken: string;
  email: string | null;
  uid: string;
  pool: string;
  tenantId: string;
}

// signInWithGoogle opens the Google OAuth popup against the GIP tenant
// picked by the BFF for the current hostname, then returns the GIP ID
// token (not the Google access token) — that's what the BFF verifies.
export async function signInWithGoogle(): Promise<SignInResult> {
  const { auth, config } = await getFirebaseAuth();
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  const cred = await signInWithPopup(auth, provider);
  const idToken = await cred.user.getIdToken(true);
  return {
    idToken,
    email: cred.user.email,
    uid: cred.user.uid,
    pool: config.pool,
    tenantId: config.tenantId,
  };
}

// exchangeForSession posts the GIP ID token to devai-auth-bff. The BFF
// verifies signature + tenant claim + admin allow-list, then sets the
// devai_session cookie on the .tesserix.app domain.
export async function exchangeForSession(
  idToken: string,
  pool: string,
  tenantId: string,
): Promise<void> {
  const res = await fetch("/auth/auto-login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      id_token: idToken,
      expected_tenant_id: tenantId,
      pool,
    }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as {
      error?: string;
      message?: string;
    };
    throw new Error(body.message ?? body.error ?? `auth-bff returned ${res.status}`);
  }
}
