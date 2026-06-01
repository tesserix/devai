# 11 — Ticketing Integration (`@Cursor` in Jira)

**Cursor parity:** Jira `@Cursor` mentions trigger cloud agents (May 19, 2026). **Priority: P2.**

## What Cursor does

Mention `@Cursor` on a Jira work item → a cloud agent activates, **scopes the work
from the ticket description + repo context**, does it, and **links the resulting
PR back** in the Jira completion update. Bugs, features, tests — all from the
ticket.

## How it works (concepts to steal)

1. **Ticket as trigger + spec.** The work item text *is* the task prompt.
2. **Context fusion.** Ticket + repo (plan 01 retrieval) → scoped task.
3. **Bidirectional link.** PR opened from the ticket; status written back.
4. **Provider‑agnostic** (Jira today; Linear / GitHub Issues are the same shape).

## DevAI mapping (framework)

This is a textbook **adapter family** — already named in `CLAUDE.md` as planned:
`adapters.ticketing` (jira, linear, github_issues).

- **`adapters/ticketing/`** ABC: `watch_mentions()`, `get_item(id)`,
  `comment(id, body)`, `link_pr(id, pr_url)`, `transition(id, state)`.
- **Mention trigger** (webhook or poll) → build a `BackgroundTask` (plan 02) with
  the ticket as prompt + repo from project mapping.
- **Context fusion** via plan 01 retrieval; plan 04 may draft a plan first.
- **Write‑back**: PR link + status comment + optional state transition, through
  the adapter (so Jira/Linear/Issues behave identically).
- Ties to plan 10 (a ticket is just another trigger into the same dispatcher).

## Implementation plan

- **Phase 1 — adapter ABC + GitHub Issues backend** (cheapest; reuses `scm/` auth).
- **Phase 2 — Jira backend** (mention webhook, work‑item API, transitions).
- **Phase 3 — trigger→task→PR→write‑back loop.**
- **Phase 4 — Linear backend** + project→repo mapping config + dashboard.

## Files & modules

```
src/devai/adapters/ticketing/{base,factory,jira,linear,github_issues,noop}.py
src/devai/webhook/app.py               # mention events
src/devai/runtime/background.py        # dispatch (plan 02)
tests/unit/test_ticketing_adapters.py
```

## Config (`DEVAI_*`)

```
DEVAI_TICKETING_PROVIDER=jira          # jira|linear|github_issues|noop
DEVAI_TICKETING_MENTION_HANDLE=@devai
DEVAI_TICKETING_PROJECT_REPO_MAP={"PROJ":"tesserix/devai"}
```

## Acceptance criteria

- Mentioning `@devai` on a ticket starts a background task scoped from the ticket.
- The opened PR is linked back on the ticket with a status comment.
- Switching provider (jira→github_issues) needs only env + creds; contract tests green.
- Misconfigured provider → Noop, no crash.

## Sources

- [Cursor Changelog — Jira integration (May 19, 2026)](https://cursor.com/changelog)
- DevAI: `CLAUDE.md` → Adapter Pattern (`adapters.ticketing` planned)
