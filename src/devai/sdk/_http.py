"""Shared httpx lazy-import for the SDK sub-clients.

httpx is the only optional dep an SDK consumer pays for. Importing it
inside one helper keeps every sub-client's surface the same: each
calls :func:`lazy_httpx` exactly once per method and gets the same
module object.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def lazy_httpx():
    """Return the imported ``httpx`` module, raising :class:`DevAIError`
    if it isn't installed. The import is local so test envs that stub
    out network code don't accidentally pull httpx."""
    try:
        import httpx  # type: ignore[import-untyped]
    except ImportError as e:
        from devai.sdk.client import DevAIError

        raise DevAIError(
            "sdk: httpx is not installed — `pip install httpx` or "
            "`pip install devai[sdk]`"
        ) from e
    return httpx
