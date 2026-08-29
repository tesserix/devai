"""Autonomous backlog watcher — polls onboarded repos for new issues and
dispatches pipeline runs, so DevAI acts on a backlog without a human trigger.

Closes the "connect + monitor + auto-detect" gap: triggering was purely reactive
(webhook) or manual (CLI / dashboard). This periodic loop — modeled on the
onboarding reconcile poller — lists each ONBOARDED repo's open issues that carry
the watch label, skips ones already processed (a Redis process-once ledger), and
dispatches a full pipeline run for each new one, capped per repo per pass so a
large backlog discovered on the first sweep doesn't stampede the queue.

Opt-in via ``DEVAI_ISSUE_WATCH_ENABLED`` (off by default — the platform stays
reactive until it's turned on). The watcher is a generic platform mechanism: it
works for any onboarded repo and the configured default blueprint; nothing here
is ALM- or agent-specific. Dependencies are injected (duck-typed) so it runs
without a live SCM / Redis / pipeline in tests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from devai.onboarding.models import OnboardingState

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

_LEDGER_TTL_SECONDS = 86400 * 30  # process-once memory window: 30 days


class IssueWatcher:
    """Periodically dispatch pipeline runs for new backlog issues.

    Injected dependencies (duck-typed):
      onboarding — ``.list_onboarded(state)`` → repos exposing ``.full_name``
      scm        — ``.list_issues(repo, state, labels, limit)`` → list[dict]
      pipeline   — ``.dispatch(intent, repo, trigger_type, label, agent_context, principal)``
      redis      — optional Redis for the process-once ledger (None → no dedup)
      config     — Settings (the ``issue_watch_*`` knobs + ``pipeline_label``)
    """

    def __init__(self, *, onboarding: Any, scm: Any, pipeline: Any, redis: Any = None, config: Settings) -> None:
        self._onboarding = onboarding
        self._scm = scm
        self._pipeline = pipeline
        self._redis = redis
        self._config = config
        self._interval = max(0, int(getattr(config, "issue_watch_interval_seconds", 300)))
        self._max_per_repo = max(1, int(getattr(config, "issue_watch_max_per_repo", 3)))

    # ── lifecycle ──────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """The poll loop — same shape as the onboarding reconcile poller."""
        await asyncio.sleep(45)  # let the SCM client + pools settle after boot
        while True:
            try:
                logger.info("issue watch: %s", await self.poll_once())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad pass never kills the loop
                logger.exception("issue watch pass failed (non-fatal)")
            if self._interval <= 0:
                return  # one-shot mode
            await asyncio.sleep(self._interval)

    # ── one pass ───────────────────────────────────────────────────────

    async def poll_once(self) -> dict[str, int]:
        repos = await self._onboarding.list_onboarded(OnboardingState.ONBOARDED)
        dispatched = 0
        for repo in repos:
            dispatched += await self._poll_repo(repo)
        return {"repos": len(repos), "dispatched": dispatched}

    async def _poll_repo(self, repo: Any) -> int:
        full = str(getattr(repo, "full_name", "") or "")
        if not full:
            return 0
        try:
            issues = await self._scm.list_issues(full, state="open", labels=self._watch_labels(), limit=100)
        except Exception:  # noqa: BLE001 — one bad repo never stops the sweep
            logger.exception("issue watch: list_issues failed for %s", full)
            return 0
        # oldest-first so the backlog is drained in order across polls
        issues = sorted(issues, key=lambda i: i.get("number") or 0)
        count = 0
        for issue in issues:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            if await self._already_processed(full, number):
                continue
            if count >= self._max_per_repo:
                logger.info("issue watch: capped at %d new issues for %s this pass", self._max_per_repo, full)
                break
            if await self._dispatch_issue(full, issue):
                await self._mark_processed(full, number, issue)
                count += 1
        return count

    def _watch_labels(self) -> list[str] | None:
        """The labels an open issue must carry. ``*`` → all issues; empty →
        fall back to pipeline_label so only opted-in issues are built."""
        raw = str(getattr(self._config, "issue_watch_labels", "") or "").strip()
        if raw == "*":
            return None
        if raw:
            return [s.strip() for s in raw.split(",") if s.strip()]
        label = str(getattr(self._config, "pipeline_label", "") or "").strip()
        return [label] if label else None

    # ── dispatch ───────────────────────────────────────────────────────

    async def _dispatch_issue(self, repo: str, issue: dict[str, Any]) -> bool:
        number = issue["number"]
        requirements = _build_requirements(issue)
        try:
            await self._pipeline.dispatch(
                intent=requirements,
                repo=repo,
                trigger_type="backlog_issue",
                label=f"backlog_issue:{number}"[:80],
                agent_context={"trigger_ref": str(number), "requirements": requirements},
                principal=_issue_principal(issue),
            )
            logger.info("issue watch: dispatched run for %s#%s", repo, number)
            return True
        except Exception:  # noqa: BLE001 — a failed dispatch retries next pass (not marked processed)
            logger.exception("issue watch: dispatch failed for %s#%s", repo, number)
            return False

    # ── process-once ledger ────────────────────────────────────────────

    @staticmethod
    def _ledger_key(repo: str, number: int) -> str:
        return f"devai:issue_watch:seen:{repo}#{number}"

    async def _already_processed(self, repo: str, number: int) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.exists(self._ledger_key(repo, number)))
        except Exception:  # noqa: BLE001
            return False

    async def _mark_processed(self, repo: str, number: int, issue: dict[str, Any]) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                self._ledger_key(repo, number), str(issue.get("updated_at") or "1"), ex=_LEDGER_TTL_SECONDS
            )
        except Exception:  # noqa: BLE001 — ledger is best-effort
            logger.debug("issue watch: ledger write failed for %s#%s", repo, number, exc_info=True)


def _build_requirements(issue: dict[str, Any]) -> str:
    """A requirements string from an issue (title + labels + body)."""
    number = issue.get("number", "?")
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    labels = [lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)]
    parts = [f"# Requirement: Issue #{number} — {title}\n"]
    if labels:
        parts.append(f"**Labels:** {', '.join(labels)}")
    parts.append(f"\n## Description\n\n{body}")

    from devai.services.guardrails import sanitize_untrusted_text

    return sanitize_untrusted_text("\n".join(parts), "issue")


def _issue_principal(issue: dict[str, Any]) -> dict[str, Any] | None:
    """Attribute the run to the issue author (login only — no email, so the run
    uses the platform default LLM, not a per-user connector). None if unknown."""
    user = issue.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else ""
    return {"login": login, "source": "backlog_issue"} if login else None


__all__ = ["IssueWatcher"]
