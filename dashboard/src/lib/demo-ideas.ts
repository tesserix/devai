import type { TrialStatus } from "./admin-api";

export interface DemoIdea {
  title: string;
  blurb: string;
  href: string;
}

export const DEMO_IDEAS: DemoIdea[] = [
  {
    title: "Run a pipeline on a repo",
    blurb: "Point DevAI at a repository and watch the ALM stages work through it end to end.",
    href: "/runs",
  },
  {
    title: "Compose a crew",
    blurb: "Assemble agents into a crew and give it a task to work through.",
    href: "/compose",
  },
  {
    title: "Try an agent in a sandbox",
    blurb: "Author an agent and evaluate it in an isolated sandbox before promoting it.",
    href: "/sandboxes",
  },
  {
    title: "Compose a workflow",
    blurb: "Chain agents into a workflow and run it against a sample task.",
    href: "/workflows",
  },
];

/** Show the onboarding panel only while there are still tokens to spend on the suggestions. */
export function shouldShowOnboarding(alreadySeen: boolean, status: TrialStatus | null | undefined): boolean {
  if (alreadySeen) return false;
  if (!status || !status.trial_enabled || !status.applicable) return false;
  return !status.exhausted;
}
