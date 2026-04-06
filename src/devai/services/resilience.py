"""Resilience utilities — retry, backoff, timeout, circuit breaker.

Production-grade error recovery for agent execution and LLM API calls.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retryable exceptions
RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential: bool = True,
    jitter: bool = True,
    retryable_exceptions: tuple[type[Exception], ...] = RETRYABLE_ERRORS,
) -> Callable:
    """Async retry decorator with exponential backoff and jitter.

    Args:
        max_attempts: Maximum number of attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exponential: Use exponential backoff (True) or constant delay (False).
        jitter: Add random jitter to prevent thundering herd.
        retryable_exceptions: Exception types that trigger a retry.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        logger.error(
                            "Retry exhausted for %s after %d attempts: %s",
                            func.__name__, max_attempts, e,
                        )
                        raise

                    delay = base_delay * (2 ** (attempt - 1)) if exponential else base_delay
                    delay = min(delay, max_delay)
                    if jitter:
                        delay *= 0.5 + random.random()

                    logger.warning(
                        "Retry %d/%d for %s after %.1fs: %s",
                        attempt, max_attempts, func.__name__, delay, e,
                    )
                    await asyncio.sleep(delay)

                except Exception:
                    # Non-retryable — fail immediately
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
    except asyncio.TimeoutError:
        logger.error("%s timed out after %.0fs", description, timeout_seconds)
        raise TimeoutError(f"{description} timed out after {timeout_seconds}s")


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
        if self._state == "open":
            if time.time() - self._last_failure_time > self.recovery_timeout:
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
        except Exception as e:
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
        clean_state = {
            k: v for k, v in state.items()
            if k != "on_progress" and not callable(v)
        }

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
        import asyncio

        keys = []
        async for key in self.redis.scan_iter(f"devai:checkpoint:{run_id}:*"):
            keys.append(key)

        if keys:
            await self.redis.delete(*keys)
