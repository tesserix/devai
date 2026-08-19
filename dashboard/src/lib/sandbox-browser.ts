export function sandboxBrowserDesktopPath(sandboxId: string): string {
  const encodedId = encodeURIComponent(sandboxId);
  const socketPath = `api/sandboxes/${encodedId}/browser/websockify`;
  const query = new URLSearchParams({ autoconnect: "true", resize: "scale", path: socketPath });
  return `/api/sandboxes/${encodedId}/browser/vnc.html?${query.toString()}`;
}
