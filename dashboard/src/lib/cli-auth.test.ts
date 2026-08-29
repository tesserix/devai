import assert from "node:assert/strict";
import test from "node:test";

import { handoffCLISignIn, parseCLIAuthRequest } from "./cli-auth.ts";

test("accepts a state-bound IPv4 loopback callback", () => {
  assert.deepEqual(
    parseCLIAuthRequest(
      "http://127.0.0.1:43127/callback",
      "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s",
    ),
    {
      callback: "http://127.0.0.1:43127/callback",
      state: "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s",
    },
  );
});

test("rejects callback exfiltration and malformed state", () => {
  const state = "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s";

  assert.equal(parseCLIAuthRequest("https://attacker.example/callback", state), null);
  assert.equal(parseCLIAuthRequest("http://localhost:43127/callback", state), null);
  assert.equal(parseCLIAuthRequest("http://127.0.0.1:43127/other", state), null);
  assert.equal(parseCLIAuthRequest("http://127.0.0.1:43127/callback?next=x", state), null);
  assert.equal(parseCLIAuthRequest("http://127.0.0.1:43127/callback", "short"), null);
});

test("posts the GIP proof only to the validated loopback callback", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetcher = async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(null, { status: 204 });
  };

  await handoffCLISignIn(
    {
      callback: "http://127.0.0.1:43127/callback",
      state: "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s",
    },
    { idToken: "gip-proof", pool: "alm", tenantId: "tenant-alm" },
    fetcher,
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:43127/callback");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    id_token: "gip-proof",
    pool: "alm",
    tenant_id: "tenant-alm",
    state: "R2hwX0aOd2Dbu5K-Rw8pXJ3Hm7_MuAqWd1H-EpKfZ8s",
  });
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.redirect, "error");
  assert.equal(calls[0].init?.referrerPolicy, "no-referrer");
});
