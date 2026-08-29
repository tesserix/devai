import { redirect } from "next/navigation";

import { newRunHref } from "@/lib/run-entry";

export default async function ComposeRedirect({
  searchParams,
}: {
  searchParams: Promise<{ repo?: string | string[] }>;
}) {
  const query = await searchParams;
  const repo = Array.isArray(query.repo) ? query.repo[0] : query.repo;
  redirect(newRunHref(repo));
}
