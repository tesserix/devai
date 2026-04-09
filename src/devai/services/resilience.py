"""Resilience utilities — retry, backoff, timeout, circuit breaker.

Production-grade error recovery for agent execution and LLM API calls.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retryable exceptions
RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)

# Rate-limit retry policy. Rate limits clear in seconds, not minutes, so
# we want to retry many times with the API-suggested delay rather than
# bail after a few exponential backoff attempts. We cap at a generous
# total wall-clock budget per call so a stuck rate-limit can't pin a
# pipeline run forever.
MAX_RATE_LIMIT_RETRIES = 12
DEFAULT_RATE_LIMIT_WAIT = 60.0  # seconds, when API doesn't tell us
MAX_RATE_LIMIT_WAIT = 120.0  # seconds, cap on a single retry-after
MAX_RATE_LIMIT_TOTAL_WAIT = 600.0  # 10 minutes total wall-clock budget per call


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Duck-type detection for rate limit errors across LLM SDKs.

    Catches anthropic.RateLimitError, openai.RateLimitError, groq's
    rate limit, generic httpx 429 responses, and string-message matches
    so we don't have to import every SDK to know its exception type.
    """
    name = type(exc).__name__
    if name == "RateLimitError":
        return True

    # httpx.HTTPStatusError / requests-style errors with a status_code
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return True

    # Some SDKs raise a generic Exception that just has the message
    msg = str(exc).lower()
    return "rate_limit_error" in msg or "rate limit" in msg or "429" in msg


def _retry_after_from_error(exc: BaseException, fallback: float = DEFAULT_RATE_LIMIT_WAIT) -> float:
    """Extract a retry-after delay from a rate-limit exception.

    Tries (in order):
      1. ``exc.response.headers["retry-after"]`` (httpx-shaped)
      2. ``exc.response.headers["x-ratelimit-reset"]`` (epoch seconds)
      3. ``exc.headers["retry-after"]``
      4. The provided fallback

    Caps the result at ``MAX_RATE_LIMIT_WAIT`` so a misbehaving server
    can't tell us to sleep for an hour.
    """

    def _read_header(headers: Any, name: str) -> str | None:
        if headers is None:
            return None
        try:
            value = headers.get(name) if hasattr(headers, "get") else headers[name]
            if value:
                return str(value)
        except (KeyError, TypeError):
            pass
        return None

    response = getattr(exc, "response", None)
    headers = None
    if response is not None:
        headers = getattr(response, "headers", None)

    raw = _read_header(headers, "retry-after") or _read_header(getattr(exc, "headers", None), "retry-after")

    if raw:
        try:
            value = float(raw)
            if value > 0:
                return min(value, MAX_RATE_LIMIT_WAIT)
        except (TypeError, ValueError):
            pass

    # x-ratelimit-reset is sometimes an epoch timestamp; convert to a
    # delta if it looks reasonable
    raw_reset = _read_header(headers, "x-ratelimit-reset")
    if raw_reset:
        try:
            value = float(raw_reset)
            now = time.time()
            if value > now:
                delta = value - now
                if delta > 0:
                    return min(delta, MAX_RATE_LIMIT_WAIT)
        except (TypeError, ValueError):
            pass

    return min(fallback, MAX_RATE_LIMIT_WAIT)


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential: bool = True,
    jitter: bool = True,
    retryable_exceptions: tuple[type[Exception], ...] = RETRYABLE_ERRORS,
) -> Callable:
    """Async retry decorator with exponential backoff and jitter.

    Two retry policies in one decorator:

    1. **Generic retryable errors** (ConnectionError, TimeoutError) get
       exponential backoff for ``max_attempts`` tries.

    2. **Rate limit errors** (any LLM SDK's 429) get a SEPARATE policy:
       up to ``MAX_RATE_LIMIT_RETRIES`` attempts, each waiting the
       API-suggested ``retry-after`` (or 60s default), capped at a
       total wall-clock budget of ``MAX_RATE_LIMIT_TOTAL_WAIT``. Rate
       limits clear in seconds — exponential backoff is the wrong tool.

    The conversation state is preserved across rate-limit retries because
    the retry happens INSIDE the API call wrapper — the message history
    sent to the model on retry is identical to the original request.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            rate_limit_retries = 0
            rate_limit_total_wait = 0.0

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Path 1: rate limit — separate retry budget so a
                    # 429 doesn't burn a generic retry slot.
                    if _is_rate_limit_error(e):
                        rate_limit_retries += 1
                        if (
                            rate_limit_retries > MAX_RATE_LIMIT_RETRIES
                            or rate_limit_total_wait >= MAX_RATE_LIMIT_TOTAL_WAIT
                        ):
                            logger.error(
                                "Rate limit retries exhausted for %s after %d attempts (%.0fs total wait): %s",
                                func.__name__,
                                rate_limit_retries,
                                rate_limit_total_wait,
                                e,
                            )
                            raise
                        wait = _retry_after_from_error(e)
                        # Don't blow past the total wall-clock cap
                        wait = min(wait, max(1.0, MAX_RATE_LIMIT_TOTAL_WAIT - rate_limit_total_wait))
                        rate_limit_total_wait += wait
                        logger.warning(
                            "Rate limit hit on %s (attempt %d/%d) — sleeping %.0fs and retrying. "
                            "Total rate-limit wait so far: %.0fs",
                            func.__name__,
                            rate_limit_retries,
                            MAX_RATE_LIMIT_RETRIES,
                            wait,
                            rate_limit_total_wait,
                        )
                        await asyncio.sleep(wait)
                        # Don't consume a generic retry attempt for a
                        # rate limit — they have their own budget.
                        continue

                    # Path 2: generic retryable errors get exponential backoff
                    if isinstance(e, retryable_exceptions):
                        last_exc = e
                        if attempt == max_attempts:
                            logger.error(
                                "Retry exhausted for %s after %d attempts: %s",
                                func.__name__,
                                max_attempts,
                                e,
                            )
                            raise

                        delay = base_delay * (2 ** (attempt - 1)) if exponential else base_delay
                        delay = min(delay, max_delay)
                        if jitter:
                            delay *= 0.5 + random.random()

                        logger.warning(
                            "Retry %d/%d for %s after %.1fs: %s",
                            attempt,
                            max_attempts,
                            func.__name__,
                            delay,
                            e,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Path 3: non-retryable — fail immediately
                    raise

            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


async def with_timeout(coro: Any, timeout_seconds: float, description: str = "operation") -> Any:
    """Execute a coroutine with a timeout.

    Args:
        coro: The coroutine to execute.
        timeout_seconds: Timeout in seconds.
        description: Description for error messages.

    Returns:
        The coroutine result.

    Raises:
        TimeoutError: If the operation exceeds the timeout.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError:
        logger.error("%s timed out after %.0fs", description, timeout_seconds)
        raise TimeoutError(f"{description} timed out after {timeout_seconds}s") from None


class CircuitBreaker:
    """Simple circuit breaker for external service calls.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Service is failing, requests are rejected immediately
    - HALF_OPEN: Testing if service recovered (allows 1 request)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        name: str = "default",
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name
        self._failures = 0
        self._state = "closed"
        self._last_failure_time = 0.0

    @property
    def state(self) -> str:
        if self._state == "open" and time.time() - self._last_failure_time > self.recovery_timeout:
            self._state = "half_open"
        return self._state

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute a function through the circuit breaker."""
        if self.state == "open":
            raise RuntimeError(f"Circuit breaker '{self.name}' is OPEN — service unavailable")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def _on_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = "open"
            logger.warning("Circuit breaker '%s' OPENED after %d failures", self.name, self._failures)


class RateLimiter:
    """Sliding-window rate limiter for LLM API calls.

    Tracks both **requests-per-minute** and **input-tokens-per-minute**
    against the provider's posted limits and proactively sleeps before
    a call that would breach either window. This is the cheap way to
    survive a Free Tier (5 RPM / 10K input-tokens/min) without
    constantly hitting 429s.

    Usage:
        limiter = RateLimiter(name="anthropic-sonnet", rpm=5, tpm=10000)
        await limiter.acquire(estimated_input_tokens=2500)
        # ... make the API call

    The estimated token count is recorded BEFORE the call. Conservative
    overestimation is fine — we want to err on the side of waiting.
    """

    def __init__(
        self,
        name: str,
        rpm: int,
        tpm: int,
        window_seconds: float = 60.0,
        safety_margin: float = 0.85,
    ) -> None:
        """
        Args:
            name: Identifier for logs
            rpm: Requests per minute (provider limit)
            tpm: Tokens per minute (provider limit)
            window_seconds: Sliding window length (default 60s)
            safety_margin: Use this fraction of the limit as the
                effective ceiling. 0.85 means we cap ourselves at 85%
                of the posted limit so two near-simultaneous calls
                don't race past it.
        """
        self.name = name
        self.rpm = max(1, int(rpm * safety_margin))
        self.tpm = max(1, int(tpm * safety_margin))
        self.window = window_seconds
        # (timestamp, tokens) tuples; pruned on every acquire
        self._calls: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()
        logger.info(
            "RateLimiter '%s' configured: %d RPM, %d TPM (with %.0f%% safety margin)",
            name,
            self.rpm,
            self.tpm,
            safety_margin * 100,
        )

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._calls = [(ts, tokens) for ts, tokens in self._calls if ts > cutoff]

    def _current_usage(self, now: float) -> tuple[int, int]:
        self._prune(now)
        return len(self._calls), sum(tokens for _, tokens in self._calls)

    async def acquire(self, estimated_input_tokens: int = 1000) -> None:
        """Block until making a request would not exceed the rate limits.

        Records the request immediately on acquisition so concurrent
        callers contribute to the same window and don't all squeak
        through at the same instant.
        """
        # Cap a single estimate to the posted TPM so a hugely-prompted
        # call doesn't loop forever waiting for budget that will never
        # exist (it'll just sleep one window and proceed; the API will
        # 429 if it really doesn't fit, and the retry layer handles it).
        estimated_input_tokens = min(estimated_input_tokens, self.tpm)

        while True:
            async with self._lock:
                now = time.monotonic()
                req_count, token_count = self._current_usage(now)

                fits_requests = req_count < self.rpm
                fits_tokens = (token_count + estimated_input_tokens) <= self.tpm

                if fits_requests and fits_tokens:
                    # Reserve the slot immediately
                    self._calls.append((now, estimated_input_tokens))
                    return

                # Compute how long to sleep until the OLDEST entry in
                # the window expires — that's the soonest a slot frees.
                oldest = min((ts for ts, _ in self._calls), default=now)
                sleep_for = max(0.5, (oldest + self.window) - now + 0.1)
                reason = []
                if not fits_requests:
                    reason.append(f"RPM ({req_count}/{self.rpm})")
                if not fits_tokens:
                    reason.append(f"TPM ({token_count}+{estimated_input_tokens}/{self.tpm})")
                logger.info(
                    "RateLimiter '%s' throttling — would exceed %s — sleeping %.1fs",
                    self.name,
                    " and ".join(reason),
                    sleep_for,
                )

            # Release the lock during sleep so other waiters can re-check
            await asyncio.sleep(sleep_for)


# Global singletons used by every LLM provider so concurrent agents
# share a single budget. Tuned for the Anthropic Free Tier (5 RPM,
# 10K input-tokens-per-minute on Sonnet/Opus/Haiku 4). Override at
# runtime via DEVAI_CLAUDE_RPM / DEVAI_CLAUDE_TPM environment variables
# when on a paid tier with higher headroom.
import os as _os  # noqa: E402

_claude_rate_limiter: RateLimiter | None = None


def get_claude_rate_limiter() -> RateLimiter:
    """Lazy global Claude rate limiter so every Claude provider instance
    shares the same budget across the whole pipeline run.
    """
    global _claude_rate_limiter
    if _claude_rate_limiter is None:
        rpm = int(_os.environ.get("DEVAI_CLAUDE_RPM", "5"))
        tpm = int(_os.environ.get("DEVAI_CLAUDE_TPM", "10000"))
        _claude_rate_limiter = RateLimiter(name="anthropic", rpm=rpm, tpm=tpm)
    return _claude_rate_limiter


def estimate_token_count(text_or_messages: Any) -> int:
    """Cheap input-token estimator.

    Uses the rough rule of thumb of 1 token per 4 characters for
    English-ish text. We don't need accuracy — we need a fast,
    conservative number to feed the rate limiter. The provider's
    actual count is what gets billed; ours is just for budgeting.
    """
    if text_or_messages is None:
        return 0
    if isinstance(text_or_messages, str):
        return max(1, len(text_or_messages) // 4)
    try:
        import json as _json

        serialized = _json.dumps(text_or_messages, default=str)
        return max(1, len(serialized) // 4)
    except Exception:
        return max(1, len(str(text_or_messages)) // 4)


class StateCheckpoint:
    """Checkpoint manager for pipeline state recovery.

    Saves pipeline state at each stage boundary so the pipeline
    can resume from the last successful stage on failure.
    """

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    async def save(self, run_id: str, stage: str, state: dict[str, Any]) -> None:
        """Save a checkpoint at a stage boundary."""
        import json

        # Don't checkpoint non-serializable items
        clean_state = {k: v for k, v in state.items() if k != "on_progress" and not callable(v)}

        key = f"devai:checkpoint:{run_id}:{stage}"
        await self.redis.set(key, json.dumps(clean_state, default=str), ex=86400 * 7)
        await self.redis.set(f"devai:checkpoint:{run_id}:latest", stage, ex=86400 * 7)

        logger.debug("Checkpoint saved: %s at stage %s", run_id, stage)

    async def load(self, run_id: str, stage: str | None = None) -> dict[str, Any] | None:
        """Load a checkpoint. If stage is None, loads the latest."""
        import json

        if stage is None:
            stage = await self.redis.get(f"devai:checkpoint:{run_id}:latest")
            if not stage:
                return None

        key = f"devai:checkpoint:{run_id}:{stage}"
        raw = await self.redis.get(key)
        if not raw:
            return None

        return json.loads(raw)

    async def get_resumable_stage(self, run_id: str) -> str | None:
        """Get the latest stage that can be resumed from."""
        return await self.redis.get(f"devai:checkpoint:{run_id}:latest")

    async def cleanup(self, run_id: str) -> None:
        """Clean up all checkpoints for a completed run."""

        keys = []
        async for key in self.redis.scan_iter(f"devai:checkpoint:{run_id}:*"):
            keys.append(key)

        if keys:
            await self.redis.delete(*keys)
