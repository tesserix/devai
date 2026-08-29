export const NEW_RUN_HREF = "/workflows?action=new";

export function newRunHref(repo?: string | null): string {
  const params = new URLSearchParams({ action: "new" });
  const normalizedRepo = repo?.trim();
  if (normalizedRepo) params.set("repo", normalizedRepo);
  return `/workflows?${params.toString()}`;
}

export function runDetailHref(runId: string): string {
  return `/runs/${encodeURIComponent(runId)}`;
}
