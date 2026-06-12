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
from typing import Any

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


# ── PII masking ──────────────────────────────────────────────────────────
# Distinct from secret redaction: personal data (emails, phone numbers, IPs)
# is masked for display/log surfaces where the full value isn't needed, while
# keeping enough to correlate (first char + domain for email).
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")
_IPV4 = re.compile(r"\b(\d{1,3})\.\d{1,3}\.\d{1,3}\.(\d{1,3})\b")


def mask_pii(text: str) -> str:
    """Mask personal data for display/log surfaces.

    - email  ``alice@corp.com``  → ``a***@corp.com`` (domain kept for ops)
    - phone  ``+1 415 555 0100``  → ``***``
    - IPv4   ``10.20.3.146``      → ``10.x.x.146``

    Returns text unchanged when there's nothing to mask. Apply on top of
    :func:`redact_secrets` for surfaces shown to other users.
    """
    if not text:
        return text
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    text = _IPV4.sub(lambda m: f"{m.group(1)}.x.x.{m.group(2)}", text)
    text = _PHONE.sub("***", text)
    return text


def mask_email(email: str) -> str:
    """First char + domain only: ``alice@corp.com`` → ``a***@corp.com``.

    For displaying a principal to OTHER users (audit feeds, A2A timelines)
    without exposing the full address. The owner still sees their own.
    """
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if local else f"***@{domain}"


def scrub(text: str) -> str:
    """Both passes: credentials redacted AND personal data masked.

    The one-call helper for any surface shown to a user other than the
    owner (cross-user feeds, audit, logs, failure comments).
    """
    return mask_pii(redact_secrets(text))


# Field names whose VALUE is always a secret regardless of content — masked
# wholesale so a key that doesn't match a known prefix still never leaks.
_SECRET_KEYS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "bot_token",
    "signing_secret",
)


def scrub_structure(obj: Any, *, _depth: int = 0) -> Any:
    """Recursively mask secrets + PII in any JSON-like structure.

    Applied to API responses that aggregate agent/tool output (A2A feeds,
    event streams) so neither a leaked credential nor personal data reaches
    another user — regardless of which nested field it landed in. A field
    whose NAME looks secret has its value masked wholesale; every other
    string is run through :func:`scrub`. Bounded recursion depth.
    """
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            key_l = str(k).lower()
            if isinstance(v, str) and any(s in key_l for s in _SECRET_KEYS):
                out[k] = "***" if v else v
            else:
                out[k] = scrub_structure(v, _depth=_depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub_structure(v, _depth=_depth + 1) for v in obj]
    return obj


__all__ = ["mask_email", "mask_pii", "redact_secrets", "scrub", "scrub_structure"]
