import assert from "node:assert/strict";
import test from "node:test";

import { sandboxBrowserDesktopPath } from "./sandbox-browser.ts";

test("browser desktop path keeps the sandbox id encoded in HTTP and WebSocket routes", () => {
  assert.equal(
    sandboxBrowserDesktopPath("sandbox /?#"),
    "/api/sandboxes/sandbox%20%2F%3F%23/browser/vnc.html?autoconnect=true&resize=scale&path=api%2Fsandboxes%2Fsandbox%2520%252F%253F%2523%2Fbrowser%2Fwebsockify",
  );
});
