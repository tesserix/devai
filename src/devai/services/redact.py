"""Redaction helpers for log lines and API responses.

Subprocess output (notably ``git clone`` failures) can echo a remote URL
that carries an embedded credential — e.g.
``https://x-access-token:ghp_xxx@github.com/owner/repo.git`` — straight
into ``stderr``. Logging or returning that raw leaks the token. Run any
externally-derived text through :func:`redact_secrets` before it lands in
a log or an HTTP response.
"""

from __future__ import annotations

import re

# URL userinfo: scheme://user:password@host  →  scheme://user:***@host
# Covers x-access-token:<tok>@ (GitHub) and oauth2:<tok>@ (GitLab).
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<secret>[^/\s@]+)@")

# Bearer / token headers occasionally surface in verbose tool output.
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{8,}")


def redact_secrets(text: str) -> str:
    """Mask credentials embedded in ``text`` (URL userinfo, bearer tokens).

    Returns the text unchanged when there's nothing to redact, so it's
    safe to wrap every log/return site unconditionally.
    """
    if not text:
        return text
    text = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    return text


__all__ = ["redact_secrets"]
