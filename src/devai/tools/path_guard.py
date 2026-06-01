"""Workspace confinement for file paths supplied to agent tools.

Document-ingestion tools read a ``file_path`` that can originate from
prompt-injected text. Without confinement an agent could be steered into
reading arbitrary files (``../../etc/passwd``) and echoing them back. When
a workspace root is configured, :func:`confine` resolves the requested
path (following ``..`` and symlinks) and rejects anything that lands
outside the root. With no root configured it's a no-op, so this is an
opt-in lock, not a behavior change.
"""

from __future__ import annotations

from pathlib import Path


class PathTraversalError(ValueError):
    """Raised when a requested path escapes the configured workspace root."""


def confine(file_path: str, root: str) -> Path:
    """Resolve ``file_path`` and ensure it stays within ``root``.

    * ``root`` empty → confinement disabled; the path is returned as-is.
    * relative path → resolved against ``root``.
    * absolute path → allowed only if it resolves inside ``root``.

    Raises :class:`PathTraversalError` when the resolved target is outside
    the root (covers ``..`` traversal, absolute escapes, and symlinks).
    """
    p = Path(file_path)
    if not root:
        return p
    base = Path(root).resolve()
    target = (p if p.is_absolute() else base / p).resolve()
    if target != base and not target.is_relative_to(base):
        raise PathTraversalError(f"path escapes workspace root: {file_path!r}")
    return target


__all__ = ["confine", "PathTraversalError"]
