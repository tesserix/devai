"use client";

import { AGENT_INFO } from "@/lib/constants";
import { ShieldQuestion } from "lucide-react";

export interface Approval {
  gate: string;
  title?: string;
  pending?: boolean;
  agent?: string;
  lane?: string;
  blueprint?: string;
  repo?: string;
  intent?: string;
  pr_number?: number | null;
  requested_at?: number | null;
  summary?: string;
  timestamp?: number;
  /** Dynamic gate context (kind=plan_approval | heal_approval). */
  kind?: string;
  questions?: string[];
  plan_summary?: string;
  tech_stack?: unknown;
  epic_issue_number?: number | null;
  /** Recovery gate (kind=heal_approval): what broke + proposed fix. */
  stage?: string;
  error?: string;
  diagnosis?: string;
}

interface ApprovalBannerProps {
  approvals: Approval[];
  onApprove: (gate: string) => void;
  onReject: (gate: string) => void;
}

/** What each well-known gate is actually asking the human to sign off on. */
const GATE_EXPLAINERS: Record<string, { what: string; approve: string; reject: string }> = {
  "plan-approval": {
    what: "Your request needs confirmation before the agents build — review the plan below.",
    approve: "Approve to build exactly this plan, end to end.",
    reject: "Reject to cancel — refine your request and run again.",
  },
  "staff-review": {
    what: "The staff reviewer finished its final pass over the generated changes.",
    approve: "Approve accepts the review — the run continues to infrastructure + deploy.",
    reject: "Reject stops the run here — nothing gets deployed.",
  },
  "deploy-release": {
    what: "Everything is built, reviewed, and tested — the run is ready to ship.",
    approve: "Approve deploys this release.",
    reject: "Reject stops before anything reaches the target environment.",
  },
};

function fmtAge(epoch?: number | null): string {
  if (!epoch) return "";
  const mins = (Date.now() / 1000 - epoch) / 60;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

export function ApprovalBanner({ approvals, onApprove, onReject }: ApprovalBannerProps) {
  // Only gates the run has actually reached. Entries without the flag are
  // legacy shapes from older runs — keep showing those (previous behavior).
  const pending = approvals.filter((a) => a.pending !== false);
  if (pending.length === 0) return null;

  return (
    <div className="space-y-2">
      {pending.map((a) => {
        const agentInfo = a.agent ? AGENT_INFO[a.agent] : undefined;
        const explain =
          GATE_EXPLAINERS[a.gate] ??
          (a.kind === "heal_approval"
            ? {
                what: `Stage "${a.stage || a.gate}" failed — the recovery agent diagnosed it and proposes a fix below.`,
                approve: "Approve to apply the fix and re-run the failed stage.",
                reject: "Reject to let the failure stand — the run stops at this stage.",
              }
            : undefined);
        return (
          <div
            key={a.gate}
            className="p-3.5 rounded-lg border"
            style={{ borderColor: "var(--warn-soft-bd)", background: "var(--warn-soft-bg)" }}
          >
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-start gap-3 min-w-0">
                <div
                  className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 mt-0.5"
                  style={{ background: "var(--surface-raised)" }}
                >
                  <ShieldQuestion className="w-4 h-4" style={{ color: "var(--warn-ink)" }} />
                </div>
                <div className="min-w-0">
                  <p className="text-sm font-semibold" style={{ color: "var(--warn-ink)" }}>
                    Your approval needed: {a.title || a.gate}
                  </p>
                  <p className="text-xs mt-1" style={{ color: "var(--ink-soft)" }}>
                    {explain?.what ??
                      `The run paused at the "${a.title || a.gate}" gate and won't continue until you decide.`}
                  </p>
                  {a.summary && (
                    <p
                      className="text-xs mt-1 italic truncate"
                      style={{ color: "var(--ink-muted)" }}
                      title={a.summary}
                    >
                      “{a.summary}”
                    </p>
                  )}
                  <div
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-[11px]"
                    style={{ color: "var(--ink-muted)" }}
                  >
                    {agentInfo && (
                      <span>
                        Agent: <span style={{ color: "var(--ink)" }}>{agentInfo.label}</span>
                      </span>
                    )}
                    {a.repo && (
                      <span className="font-mono truncate" style={{ maxWidth: 220 }} title={a.repo}>
                        {a.repo}
                      </span>
                    )}
                    {typeof a.pr_number === "number" && a.repo && (
                      <a
                        href={`https://github.com/${a.repo}/pull/${a.pr_number}`}
                        target="_blank"
                        rel="noreferrer"
                        className="underline"
                        style={{ color: "var(--accent)" }}
                      >
                        Review PR #{a.pr_number} ↗
                      </a>
                    )}
                    {a.requested_at ? <span>waiting {fmtAge(a.requested_at)}</span> : null}
                  </div>
                  {a.intent && (
                    <p
                      className="text-[11px] mt-1.5 truncate"
                      style={{ color: "var(--ink-muted)" }}
                      title={a.intent}
                    >
                      Request: {a.intent}
                    </p>
                  )}
                  {a.kind === "heal_approval" && (
                    <div
                      className="mt-2 rounded p-2.5 space-y-1.5"
                      style={{ background: "var(--surface-raised)", fontSize: 12 }}
                    >
                      {a.error && (
                        <p className="font-mono" style={{ color: "var(--error-ink)", maxHeight: 80, overflowY: "auto" }}>
                          {a.error}
                        </p>
                      )}
                      {a.diagnosis && (
                        <p style={{ color: "var(--ink-soft)" }}>
                          <span style={{ color: "var(--ink-muted)" }}>Diagnosis: </span>
                          {a.diagnosis}
                        </p>
                      )}
                      {a.plan_summary && (
                        <p className="whitespace-pre-wrap" style={{ color: "var(--ink-soft)", maxHeight: 110, overflowY: "auto" }}>
                          <span style={{ color: "var(--ink-muted)" }}>Proposed fix: </span>
                          {a.plan_summary}
                        </p>
                      )}
                      {(a.questions ?? []).length > 0 && (
                        <div>
                          <p style={{ color: "var(--warn-ink)", fontWeight: 600 }}>Decision needed:</p>
                          <ul className="list-disc ml-4" style={{ color: "var(--ink-soft)" }}>
                            {(a.questions ?? []).map((q, i) => (
                              <li key={i}>{q}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                  {a.kind === "plan_approval" && (
                    <div
                      className="mt-2 rounded p-2.5 space-y-1.5"
                      style={{ background: "var(--surface-raised)", fontSize: 12 }}
                    >
                      {a.tech_stack ? (
                        <p style={{ color: "var(--ink-soft)" }}>
                          <span style={{ color: "var(--ink-muted)" }}>Proposed tech: </span>
                          {typeof a.tech_stack === "string" ? a.tech_stack : JSON.stringify(a.tech_stack).slice(0, 160)}
                        </p>
                      ) : null}
                      {a.plan_summary && (
                        <p
                          className="whitespace-pre-wrap"
                          style={{ color: "var(--ink-soft)", maxHeight: 130, overflowY: "auto" }}
                        >
                          {a.plan_summary}
                        </p>
                      )}
                      {(a.questions ?? []).length > 0 && (
                        <div>
                          <p style={{ color: "var(--warn-ink)", fontWeight: 600 }}>Please confirm:</p>
                          <ul className="list-disc ml-4" style={{ color: "var(--ink-soft)" }}>
                            {(a.questions ?? []).map((q, i) => (
                              <li key={i}>{q}</li>
                            ))}
                          </ul>
                          <p className="mt-1 text-[11px]" style={{ color: "var(--ink-muted)" }}>
                            Approving accepts the agents' assumptions on these points.
                          </p>
                        </div>
                      )}
                      {typeof a.epic_issue_number === "number" && a.repo && (
                        <a
                          href={`https://github.com/${a.repo}/issues/${a.epic_issue_number}`}
                          target="_blank"
                          rel="noreferrer"
                          className="underline"
                          style={{ color: "var(--accent)" }}
                        >
                          View epic #{a.epic_issue_number} ↗
                        </a>
                      )}
                    </div>
                  )}
                </div>
              </div>
              <div className="flex flex-col items-end gap-1.5 shrink-0">
                <div className="flex gap-2">
                  <button
                    onClick={() => onReject(a.gate)}
                    title={explain?.reject ?? "Stop the run at this gate"}
                    className="px-3 py-1.5 text-xs font-medium rounded-md"
                    style={{
                      color: "var(--error-ink)",
                      background: "var(--error-soft-bg)",
                      border: "1px solid var(--error-soft-bd)",
                    }}
                  >
                    Reject
                  </button>
                  <button
                    onClick={() => onApprove(a.gate)}
                    title={explain?.approve ?? "Let the run continue past this gate"}
                    className="px-3 py-1.5 text-xs font-medium rounded-md"
                    style={{
                      color: "var(--ok-ink)",
                      background: "var(--ok-soft-bg)",
                      border: "1px solid var(--ok-soft-bd)",
                    }}
                  >
                    Approve
                  </button>
                </div>
                <span className="text-[10px] text-right" style={{ color: "var(--ink-muted)" }}>
                  {explain ? explain.approve : "Approve continues · Reject stops the run"}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
