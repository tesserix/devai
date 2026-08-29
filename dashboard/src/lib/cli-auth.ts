export type CLIAuthRequest = {
  callback: string;
  state: string;
};

type SignInProof = {
  idToken: string;
  pool: string;
  tenantId: string;
};

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

type LoopbackRequestInit = RequestInit & {
  targetAddressSpace: "loopback";
};

const STATE_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export function parseCLIAuthRequest(
  callbackValue: string | null,
  stateValue: string | null,
): CLIAuthRequest | null {
  if (!callbackValue || !stateValue || !STATE_PATTERN.test(stateValue)) return null;

  let callback: URL;
  try {
    callback = new URL(callbackValue);
  } catch {
    return null;
  }
  const port = Number(callback.port);
  if (
    callback.protocol !== "http:" ||
    callback.hostname !== "127.0.0.1" ||
    callback.pathname !== "/callback" ||
    callback.username !== "" ||
    callback.password !== "" ||
    callback.search !== "" ||
    callback.hash !== "" ||
    !Number.isInteger(port) ||
    port < 1024 ||
    port > 65535
  ) {
    return null;
  }
  return { callback: callback.toString(), state: stateValue };
}

export async function handoffCLISignIn(
  request: CLIAuthRequest,
  proof: SignInProof,
  fetcher: Fetcher = fetch,
): Promise<void> {
  const init: LoopbackRequestInit = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id_token: proof.idToken,
      pool: proof.pool,
      tenant_id: proof.tenantId,
      state: request.state,
    }),
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    referrerPolicy: "no-referrer",
    signal: AbortSignal.timeout(10_000),
    targetAddressSpace: "loopback",
  };
  const response = await fetcher(request.callback, init);
  if (!response.ok) throw new Error("DevAI CLI did not accept the sign-in proof");
}
