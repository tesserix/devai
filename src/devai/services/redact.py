"""Redaction helpers for log lines and API responses.

Subprocess output (notably ``git clone`` failures) can echo a remote URL
that carries an embedded credential — e.g.
``https://x-access-token:ghp_xxx@github.com/owner/repo.git`` — straight
into ``stderr``. Logging or returning that raw leaks the token. Run any
externally-derived text through :func:`redact_secrets` before it lands in
a log or an HTTP response.

Beyond URL userinfo and bearer headers, exception strings and tool output
can also carry *bare* provider keys (an Anthropic ``sk-ant-...`` key, a
GitHub ``ghp_...`` PAT, a LangSmith ``lsv2_pt_...`` key, an AWS access-key
id, a Slack bot token, a GitLab PAT) or ``api_key=...`` / ``password=...``
style fragments. Those are echoed to the chat client and posted as failure
comments on public GitHub issues, so they must be masked too.
"""

from __future__ import annotations

import re

# URL userinfo: scheme://user:password@host  →  scheme://user:***@host
# Covers x-access-token:<tok>@ (GitHub) and oauth2:<tok>@ (GitLab).
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<secret>[^/\s@]+)@")

# Bearer / token headers occasionally surface in verbose tool output.
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{8,}")

# Bare provider keys, identified by their distinctive prefix. Each pattern is
# paired with the literal prefix to keep in the masked output so an operator can
# still tell *which* credential leaked. Ordered so longer/more-specific prefixes
# (``sk-ant-``, ``lsv2_pt_``) are tried before their shorter cousins (``sk-``).
#
#   sk-ant-…   Anthropic API key            sk-…       OpenAI / generic
#   ghp_/gho_/ghu_/ghs_…  GitHub tokens     lsv2_pt_…  LangSmith key
#   xoxb-…     Slack bot token              AKIA…      AWS access-key id
#   glpat-…    GitLab personal-access token
_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9._\-]{8,}"), "sk-ant-"),
    (re.compile(r"lsv2_pt_[A-Za-z0-9._\-]{8,}"), "lsv2_pt_"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), ""),  # prefix is the matched gh?_
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{8,}"), "xoxb-"),
    (re.compile(r"\bglpat-[A-Za-z0-9._\-]{8,}"), "glpat-"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), ""),  # no separator — collapse to ***
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"), "sk-"),
)

# ``password=secret`` / ``api_key: secret`` / ``secret="..."`` style fragments,
# tolerant of an ``=`` or ``:`` separator and optional surrounding quotes. Only
# the value is masked; the field name is preserved. The value class deliberately
# excludes ``@`` ``/`` ``\`` so it can't swallow the tail of an already-masked
# URL (``token:***@github.com/...``) — URL userinfo is handled separately above.
_SECRET_FIELD = re.compile(
    r"(?i)(?P<field>\b(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?token|token))"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\s\"',;&@/\\]{4,})(?P=quote)"
)


def redact_secrets(text: str) -> str:
    """Mask credentials embedded in ``text``.

    Covers URL userinfo, bearer/token headers, bare provider keys (Anthropic,
    OpenAI, GitHub, LangSmith, Slack, AWS, GitLab) and ``field=value`` secret
    fragments. Returns the text unchanged when there's nothing to redact, so
    it's safe to wrap every log/return site unconditionally.
    """
    if not text:
        return text
    text = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:***@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    for pattern, prefix in _PREFIX_PATTERNS:
        text = pattern.sub(lambda m, _p=prefix: _mask_prefix(m, _p), text)
    text = _SECRET_FIELD.sub(r"\g<field>\g<sep>\g<quote>***\g<quote>", text)
    return text


def _mask_prefix(match: re.Match[str], prefix: str) -> str:
    """Replace a matched bare key with ``<prefix>***``, keeping the prefix.

    For the GitHub family the prefix is the matched ``gh?_`` (first four chars);
    keys with no separator (``AKIA``) collapse entirely to ``***``.
    """
    if prefix:
        return f"{prefix}***"
    token = match.group(0)
    if token.startswith("gh") and len(token) >= 4 and token[3] == "_":
        return f"{token[:4]}***"
    return "***"


__all__ = ["redact_secrets"]
