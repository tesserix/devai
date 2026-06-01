# 03 — ReviewBot + Autofix (BugBot parity)

**Cursor parity:** BugBot event‑driven PR review + Autofix. **Priority: P0 (flagship).**

> **Durability ([00 Temporal](00-temporal-orchestration.md)):** the review is a
> Workflow; each autofix is a **child workflow** (bounded retry); a new commit
> Signals the parent to re‑review. The whole loop survives restarts and won't
> re‑post duplicate comments (idempotency keyed on PR + finding).

## What Cursor does

On every PR open/update, an agent reviews the diff, reasons about it *with the
surrounding codebase* (not just the patch), flags real bugs as inline comments,
and — with Autofix — spins a cloud agent that commits fixes back to the PR branch.
Fully event‑driven; no manual trigger.

## How it works (the concepts to steal)

1. **Webhook‑driven, zero‑touch.** PR event → start review automatically.
2. **Diff + dynamic context pull.** The agent reads the diff, then *follows code
   paths* — opens files outside the diff, pulls related symbols (this is where
   plan 01 retrieval pays off).
3. **Multi‑model / multi‑pass verification.** Candidate issues are
   cross‑checked to suppress plausible‑but‑wrong findings (adversarial verify).
4. **Inline, actionable output.** Comments land on exact lines.
5. **Autofix loop.** Confirmed issue → background agent (plan 02) fixes it →
   commit to the PR branch → re‑review.

## DevAI mapping (framework)

- **Triggers already exist:** `webhook/` handles GitHub/GitLab/ADO PR events.
- **Reviewers already exist:** `StaffReviewer` + `SecurityExpert` agents. Compose
  them into a `review` blueprint with an **adversarial verify** stage (we already
  loop `review_code` and hard‑gate `security_scan`).
- **Context** from plan 01 retrieval + `scm/` file reads beyond the diff.
- **Inline comments** via the `scm/` layer (add `create_review_comment(path,
  line, body)` to the SCM ABC; translate to GH review API / GL discussions / ADO
  threads).
- **Autofix** = emit a `BackgroundTask` (plan 02) scoped to the finding → PR
  commit → A2A `notification` re‑triggers review.
- Findings persisted to `security_findings` / a new `review_findings`, shown on
  the dashboard a2a/review feed.

## Implementation plan

- **Phase 1 — review blueprint.** `blueprints/pr-review` (already referenced by
  `DEVAI_PIPELINE_PR_REVIEW_BLUEPRINT`): fetch diff → retrieve context → reviewers
  in parallel → adversarial verify → dedupe.
- **Phase 2 — inline comments.** Extend SCM ABC + 3 backends; map findings→lines.
- **Phase 3 — autofix.** Confirmed, high‑confidence findings → `BackgroundTask`;
  fix branch commit; re‑review loop with a max‑rounds cap (reuse review iteration
  cap).
- **Phase 4 — controls.** Per‑repo enable, severity threshold for autofix, "draft
  PR until ready" (the onboarding `draft` flag pattern already exists).

## Files & modules

```
blueprints/pr-review/*.yaml
src/devai/agents/{staff_reviewer,security_expert}.py   # exist — compose
src/devai/scm/base.py + {github,gitlab,azure_devops}_client.py  # +review comments
src/devai/webhook/app.py                                # PR event → review
src/devai/review/{verify,dedupe,autofix}.py
tests/unit/test_pr_review.py
```

## Config (`DEVAI_*`)

```
DEVAI_REVIEWBOT_ENABLED=true
DEVAI_REVIEWBOT_AUTOFIX=true
DEVAI_REVIEWBOT_AUTOFIX_MIN_CONFIDENCE=0.8
DEVAI_REVIEWBOT_MAX_ROUNDS=3
DEVAI_PIPELINE_PR_REVIEW_BLUEPRINT=pr-review
```

## Acceptance criteria

- Opening a PR auto‑posts inline comments on real issues; no comment on a clean PR.
- Verify stage suppresses ≥1 seeded false positive (adversarial check works).
- With Autofix on, a seeded bug gets a fix commit on the PR branch and re‑review
  passes.
- Disabled per‑repo → no webhook side effects.

## Sources

- [Building a better Bugbot · Cursor](https://cursor.com/blog/building-bugbot)
- [Closing the loop with Bugbot Autofix · Cursor](https://cursor.com/blog/bugbot-autofix)
- [Using Cursor Bugbot to autoreview PRs · WorkOS](https://workos.com/blog/cursor-bugbot-autoreview-claude-code-prs)
