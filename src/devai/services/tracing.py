"""LangSmith tracing integration for DevAI agents.

Provides zero-overhead tracing when disabled — all decorators and wrappers
become no-ops if LANGCHAIN_TRACING_V2 is not set to "true".

Environment variables (set via Settings.export_langsmith_env):
    LANGCHAIN_TRACING_V2=true
    LANGCHAIN_API_KEY=lsv2_pt_xxx
    LANGCHAIN_PROJECT=devai

When enabled, LangSmith creates a nested trace hierarchy:

    [chain] alm_pipeline.run (45s)
    ├── [chain] requirements_analyst (3s)
    ├── [chain] product_director (8s)
    │   └── [llm] o3 (6s)
    ├── [chain] engineering_manager (12s)
    │   └── [llm] claude-sonnet-4 (10s)
    ├── [chain] senior_developer (15s)
    │   └── [llm] claude-sonnet-4 (12s)
    ├── [chain] staff_reviewer (5s)
    ├── [chain] qa_tester (8s)
    ├── [chain] ci_monitor (3s)
    └── [chain] release_manager (2s)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_TRACING_ENABLED = False

F = TypeVar("F", bound=Callable[..., Any])


def init_langsmith() -> None:
    """Initialize LangSmith tracing if configured."""
    global _TRACING_ENABLED

    tracing_v2 = os.environ.get("LANGCHAIN_TRACING_V2", "").lower()
    api_key = os.environ.get("LANGCHAIN_API_KEY", "")
    project = os.environ.get("LANGCHAIN_PROJECT", "devai")

    if tracing_v2 != "true" or not api_key:
        logger.info(
            "langsmith_disabled",
            extra={"reason": "LANGCHAIN_TRACING_V2 not 'true' or no API key"},
        )
        return

    try:
        import langsmith  # noqa: F401

        _TRACING_ENABLED = True
        logger.info(
            "langsmith_enabled",
            extra={"project": project, "api_key_set": True},
        )
    except ImportError:
        logger.warning(
            "langsmith_not_installed",
            extra={"hint": "pip install langsmith"},
        )


def is_tracing_enabled() -> bool:
    """Check if LangSmith tracing is active."""
    return _TRACING_ENABLED


def wrap_anthropic_client(client: Any) -> Any:
    """Wrap Anthropic client for automatic LangSmith tracing."""
    if not _TRACING_ENABLED:
        return client

    try:
        # Try the dedicated Anthropic wrapper first (langsmith >= 0.1.x)
        from langsmith.wrappers import wrap_anthropic

        return wrap_anthropic(client)
    except (ImportError, AttributeError):
        pass

    try:
        # Fallback to OpenAI-compatible wrapper
        from langsmith.wrappers import wrap_openai

        return wrap_openai(client)
    except (ImportError, Exception):
        return client


def wrap_openai_client(client: Any) -> Any:
    """Wrap AsyncOpenAI client for automatic LangSmith tracing."""
    if not _TRACING_ENABLED:
        return client

    try:
        from langsmith.wrappers import wrap_openai

        return wrap_openai(client)
    except ImportError:
        return client


def traceable_if_enabled(
    name: str,
    run_type: str = "chain",
    metadata: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """Conditional @traceable decorator — zero overhead when LangSmith is disabled.

    Args:
        name: Trace span name (e.g., "agent.product_director").
        run_type: LangSmith run type ("chain", "llm", "tool", "retriever").
        metadata: Extra metadata to attach to the trace.
    """

    def decorator(fn: F) -> F:
        if not _TRACING_ENABLED:
            return fn

        try:
            from langsmith import traceable

            traced = traceable(name=name, run_type=run_type, metadata=metadata or {})(fn)
            return traced  # type: ignore[return-value]
        except ImportError:
            return fn

    return decorator
