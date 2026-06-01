"""Shared A2A exception type (kept in its own module so the trust-check
helpers and the client can both import it without a cycle)."""

from __future__ import annotations


class A2AError(Exception):
    """Raised when card resolution, trust verification, or an A2A call fails."""
