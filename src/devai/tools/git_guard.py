"""Repo/ref validation + hardened ``git clone`` for agent tools.

Several agent tools (security, validation, test) clone a caller-supplied
``repo``/``branch`` into a tempdir before running scanners. The values can
originate from prompt-injected requirement text, so a ``branch`` like
``--upload-pack=…`` or a ``repo`` containing ``..`` must never reach git as
an option/argument. This module mirrors the validation already enforced in
the runtime path (``runtime.job_spec``) so the tool boundary is just as
strict:

* :func:`validate_repo` / :func:`validate_ref` reject ``..`` and leading
  ``-`` (git option injection) plus anything outside the safe charset.
* :func:`run_git_clone` always terminates option parsing with ``--``,
  disables interactive credential prompts (``GIT_TERMINAL_PROMPT=0``) and
  blocks any transport other than https (``protocol.allow=never`` +
  ``protocol.https.allow=always``). It uses ``create_subprocess_exec`` (no
  shell), so there is no string interpolation to escape.

This is a hardening of an existing capability — clones already happened; we
only constrain what can be cloned and how — so it does not change behaviour
for legitimate ``owner/name`` + branch inputs.
"""

from __future__ import annotations

import asyncio
import os
import re

#   repo:   owner/name  (GitHub "owner/repo" form)
#   ref:    a branch/tag name (no leading '-', no shell metacharacters)
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")


class InvalidGitRef(ValueError):
    """Raised when a tool-supplied repo slug or git ref is unsafe."""


def validate_repo(repo: str) -> str:
    """Return ``repo`` if it is a safe ``owner/name`` slug, else raise.

    Rejecting ``..`` and a leading ``-`` closes path-traversal and
    git-option-injection at the tool boundary.
    """
    r = (repo or "").strip()
    if not r or ".." in r or r.startswith("-") or not _REPO_RE.match(r):
        raise InvalidGitRef(f"invalid repo slug: {repo!r} (expected owner/name)")
    return r


def validate_ref(ref: str) -> str:
    """Return ``ref`` if it is a safe branch/tag name, else raise.

    A leading ``-`` would be parsed by git as an option, and ``..`` could
    reference a parent path, so both are rejected.
    """
    r = (ref or "").strip()
    if not r or ".." in r or not _REF_RE.match(r):
        raise InvalidGitRef(f"invalid git ref: {ref!r}")
    return r


async def run_git_clone(
    clone_url: str,
    branch: str,
    dest: str,
    *,
    depth: int = 1,
) -> tuple[int, bytes]:
    """Run a hardened, shallow ``git clone``; return ``(returncode, stderr)``.

    ``branch`` is validated before use; ``--`` terminates option parsing so
    the URL/dest can never be read as flags. The caller already validated
    ``clone_url`` is derived from a checked ``repo`` slug. Returning the
    captured stderr lets callers log a redacted failure without a second
    ``communicate()``.
    """
    validate_ref(branch)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "clone",
        "--branch",
        branch,
        "--depth",
        str(depth),
        "--",
        clone_url,
        dest,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # GIT_TERMINAL_PROMPT=0 stops git hanging on an interactive
        # credential prompt for a private repo / bad token.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    _, stderr = await proc.communicate()
    return proc.returncode or 0, stderr


__all__ = ["InvalidGitRef", "validate_repo", "validate_ref", "run_git_clone"]
