/**
 * Path builders and the trial tone rule, kept free of `fetch` so they are
 * directly unit-testable. `api.admin.*` in api.ts consumes these.
 */

export interface TrialStatus {
  trial_enabled: boolean;
  budget: number;
  used: number;
  remaining: number;
  exhausted: boolean;
  warning: boolean;
  has_own_connector?: boolean;
  applicable?: boolean;
}

export type TrialTone = "hidden" | "ok" | "warning" | "exhausted";

export function adminOverviewPath(days = 30): string {
  return `/admin/overview?days=${days}`;
}

export function adminOpenPanelPath(days = 30): string {
  return `/admin/openpanel?days=${days}`;
}

/**
 * Which trial treatment to show. The backend already computes `warning`
 * at >=80% and `exhausted`, so this maps rather than re-derives — one
 * threshold, defined server-side in settings/trial.py.
 */
export function trialTone(status: TrialStatus | null | undefined): TrialTone {
  if (!status || !status.trial_enabled) return "hidden";
  if (status.exhausted) return "exhausted";
  if (status.warning) return "warning";
  return "ok";
}
