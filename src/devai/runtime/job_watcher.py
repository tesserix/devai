"""Background watcher that surfaces K8s Job completion to awaiting stages.

The lifecycle is:

  1. A `JobRunnerStage` creates a Job, then calls
     `JobWatcher.register(job_name) -> asyncio.Event`.
  2. The watcher's background loop tails Job events via the kubernetes
     Watch API. When a Job reaches a terminal phase (Complete / Failed),
     it stores the outcome in `self._outcomes[job_name]` and sets the
     Event so the stage returns.

A single watcher instance is shared across the PipelineService; it runs
for the lifetime of the api pod. On api restart, in-flight Jobs keep
executing on the cluster but their stages are forgotten — recovery is a
phase-2 concern that we'll layer on top of this once the basic
end-to-end works (the executor will resume stages on a fresh pod by
re-creating the awaiter and reading Job status from the cluster).

The watcher tolerates transient cluster outages: the inner loop catches
exceptions, sleeps for a short backoff, and resumes. It logs but never
crashes the api pod.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.runtime.k8s_client import K8sJobRuntime

logger = logging.getLogger(__name__)


# Cap how much pod stdout we pull and retain. The runner can emit a large log
# (and on failure the tail is echoed into exception messages / the dashboard),
# so bound it to avoid unbounded memory growth from a noisy or hostile runner.
_MAX_LOG_TAIL_LINES = 2000
_MAX_LOG_TAIL_BYTES = 256 * 1024  # 256 KiB is plenty for triage


def _cap_logs(logs: str) -> str:
    """Return the trailing slice of ``logs`` bounded to _MAX_LOG_TAIL_BYTES."""
    if not logs:
        return ""
    if len(logs) <= _MAX_LOG_TAIL_BYTES:
        return logs
    return logs[-_MAX_LOG_TAIL_BYTES:]


# Bound the parked-outcome cache so a stream of completed-but-unclaimed Jobs
# (e.g. stages that were forgotten after an api restart) can't grow memory
# without limit. Outcomes older than the TTL or beyond the cap are evicted on
# each new parking — a late-registering stage past the TTL re-derives state
# from the cluster instead (phase-2 resume).
_OUTCOME_TTL_SECONDS = 3600.0  # 1h — well past any normal stage await
_OUTCOME_MAX_ENTRIES = 512


@dataclass(slots=True)
class JobOutcome:
    """The Job's terminal state, surfaced to the awaiting stage."""

    job_name: str
    succeeded: bool
    message: str = ""
    pod_name: str | None = None
    logs_tail: str = ""
    raw_status: dict[str, Any] = field(default_factory=dict)
    # Parsed from the pod's LOG::/CHECKPOINT::/RESULT:: line-protocol.
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    # Monotonic timestamp the outcome was parked at — drives TTL eviction.
    parked_at: float = field(default_factory=time.monotonic)


class JobWatcher:
    """Long-running watcher over Jobs in the runtime namespace.

    Stages register interest in a Job name; the watcher fires their
    `asyncio.Event` and parks the `JobOutcome` for the stage to read.
    """

    def __init__(self, runtime: K8sJobRuntime) -> None:
        self._runtime = runtime
        # job_name → asyncio.Event waiting for completion
        self._waiters: dict[str, asyncio.Event] = {}
        # job_name → JobOutcome (set just before the Event fires)
        self._outcomes: dict[str, JobOutcome] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Watch loop backoff on transient errors. Capped to 30s.
        self._backoff_seconds = 1.0

    # ── public surface ───────────────────────────────────────────────

    def register(self, job_name: str) -> asyncio.Event:
        """Subscribe to completion of `job_name`.

        Returns an Event the caller can `await event.wait()` on. The
        watcher fires it when the Job hits Complete/Failed. After firing,
        the caller MUST call `consume(job_name)` to free the slot.
        """
        ev = self._waiters.get(job_name)
        if ev is None:
            ev = asyncio.Event()
            self._waiters[job_name] = ev
        return ev

    def consume(self, job_name: str) -> JobOutcome | None:
        """Read the outcome and free the watcher's slot.

        Idempotent — returns None if nothing was parked (e.g. the
        watcher was restarted before the Job completed)."""
        outcome = self._outcomes.pop(job_name, None)
        self._waiters.pop(job_name, None)
        return outcome

    def _evict_stale_outcomes(self) -> None:
        """Drop expired / overflow parked outcomes that no one claimed.

        Outcomes are only consumed by a registered awaiter. If a stage was
        forgotten (api restart) its outcome would otherwise live forever, so
        evict any whose ``parked_at`` exceeds the TTL, and — if still over the
        entry cap — drop the oldest. Outcomes with a live waiter are kept so we
        never strand an in-flight stage.
        """
        now = time.monotonic()
        for name, outcome in list(self._outcomes.items()):
            if name in self._waiters:
                continue
            if now - outcome.parked_at > _OUTCOME_TTL_SECONDS:
                self._outcomes.pop(name, None)
        if len(self._outcomes) <= _OUTCOME_MAX_ENTRIES:
            return
        # Over the cap — evict oldest unwaited outcomes first.
        evictable = sorted(
            (item for item in self._outcomes.items() if item[0] not in self._waiters),
            key=lambda item: item[1].parked_at,
        )
        overflow = len(self._outcomes) - _OUTCOME_MAX_ENTRIES
        for name, _ in evictable[:overflow]:
            self._outcomes.pop(name, None)

    async def start(self) -> None:
        """Spawn the background watch loop. Safe to call multiple times."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="devai-job-watcher")
        logger.info("job watcher: background loop started")

    async def stop(self) -> None:
        """Signal the loop to exit and wait for it to drain."""
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                logger.warning("job watcher: loop didn't drain in 5s — cancelling")
                self._task.cancel()
                try:
                    await self._task
                except Exception:  # noqa: BLE001
                    pass
        self._task = None

    # ── internal: the watch loop ─────────────────────────────────────

    async def _loop(self) -> None:
        """Stream Job events forever (until stop()).

        Uses kubernetes_asyncio.watch which yields one event per Job
        revision. We filter to our label selector to avoid noise from
        other workloads in the namespace.
        """
        try:
            from kubernetes_asyncio import watch
        except ImportError:
            logger.error("job watcher: kubernetes_asyncio missing — loop exiting")
            return

        label_selector = "devai.tesserix.app/role=runner"
        cfg = self._runtime.config

        while not self._stopping.is_set():
            w = watch.Watch()
            try:
                async with w.stream(
                    self._runtime.batch_v1.list_namespaced_job,
                    namespace=cfg.namespace,
                    label_selector=label_selector,
                    timeout_seconds=300,  # cycle every 5 min — Watch can stall otherwise
                ) as stream:
                    async for event in stream:
                        if self._stopping.is_set():
                            break
                        await self._handle_event(event)
                # Stream timed out cleanly — reset backoff and re-enter.
                self._backoff_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — keep the watcher alive
                logger.exception("job watcher: stream raised — backing off")
                await asyncio.sleep(self._backoff_seconds)
                self._backoff_seconds = min(self._backoff_seconds * 2, 30.0)
            finally:
                try:
                    await w.close()
                except Exception:  # noqa: BLE001
                    pass

        logger.info("job watcher: loop drained")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Inspect a Job event; signal waiters if it reached a terminal state."""
        obj = event.get("object")
        if obj is None:
            return
        # `object` may be a dict (raw) or a model instance (typed). Handle
        # both — different SDK paths return different shapes.
        job_name = _safe_get(obj, ["metadata", "name"])
        status = _safe_get(obj, ["status"]) or {}

        if job_name is None:
            return

        succeeded = bool(status.get("succeeded") or 0)
        failed = bool(status.get("failed") or 0)
        conditions = status.get("conditions") or []

        # Use conditions when available — `succeeded`/`failed` counters are
        # noisy on retried pods.
        condition_type = None
        condition_message = ""
        for cond in conditions:
            if cond.get("type") in ("Complete", "Failed") and cond.get("status") == "True":
                condition_type = cond["type"]
                condition_message = cond.get("message", "")
                break

        if condition_type is None and not (succeeded or failed):
            # Still running — nothing to do.
            return

        # We only act once per Job. If no waiter has registered yet, park
        # the outcome anyway so a late-registering stage can still claim it.
        if job_name in self._outcomes:
            return

        ok = condition_type == "Complete" or (succeeded and not failed)
        pod_name = await self._runtime.find_pod_for_job(job_name)
        logs = ""
        if pod_name is not None:
            logs = _cap_logs(await self._runtime.pod_logs(pod_name, tail_lines=_MAX_LOG_TAIL_LINES))

        # Recover the structured result + checkpoint timeline from the pod's
        # line-protocol stdout (LOG::/CHECKPOINT::/RESULT::).
        from devai.runtime.protocol import parse_runner_lines

        parsed = parse_runner_lines(logs)

        outcome = JobOutcome(
            job_name=job_name,
            succeeded=ok,
            message=condition_message or ("complete" if ok else "failed"),
            pod_name=pod_name,
            logs_tail=logs,
            raw_status=status if isinstance(status, dict) else {},
            checkpoints=parsed.checkpoints,
            result=parsed.result,
        )
        # Trim stale/unclaimed outcomes before adding a new one so the cache
        # stays bounded even when stages are forgotten across api restarts.
        self._evict_stale_outcomes()
        self._outcomes[job_name] = outcome

        ev = self._waiters.get(job_name)
        if ev is not None:
            ev.set()
            logger.info(
                "job watcher: %s reached %s — outcome parked",
                job_name,
                "Complete" if ok else "Failed",
            )
        else:
            logger.debug(
                "job watcher: %s done with no waiter — outcome cached for late pickup",
                job_name,
            )


def _safe_get(obj: Any, path: list[str]) -> Any:
    """Walk a dotted path through dicts or model attributes.

    The kubernetes SDK returns typed models for `list_namespaced_job`
    streams but dicts for raw watch streams; this normalises both.
    """
    cur = obj
    for key in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return cur


__all__ = ["JobOutcome", "JobWatcher"]
