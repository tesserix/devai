"""Server-side OpenPanel reader for the admin overview.

OpenPanel is the ClickHouse-backed web analytics already deployed in the
cluster. It answers what the backend cannot — page hits, sessions,
referrers — but it is client-side instrumented, so its numbers are
approximate (ad-blockers undercount) and the page labels them as such.

Two deliberate properties:
  - The client secret is used here and never sent to the browser; the
    dashboard reaches OpenPanel only through this proxy.
  - Nothing raises. Unconfigured or unreachable both return
    {"enabled": False, ...}, so this ships and passes its tests before
    DevAI is onboarded as an OpenPanel project in tesserix-k8s.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


async def fetch_overview(config: Any, days: int) -> dict[str, Any]:
    """Visitor/session rollup from OpenPanel, or a disabled marker."""
    api_url = (getattr(config, "openpanel_api_url", "") or "").rstrip("/")
    client_id = getattr(config, "openpanel_client_id", "") or ""
    client_secret = getattr(config, "openpanel_client_secret", "") or ""
    if not (api_url and client_id and client_secret):
        return {"enabled": False, "reason": "not configured"}

    try:
        import httpx  # noqa: PLC0415 — lazy per the adapter convention

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            res = await client.get(
                f"{api_url}/export/overview",
                params={"days": int(days)},
                headers={
                    "openpanel-client-id": client_id,
                    "openpanel-client-secret": client_secret,
                },
            )
            res.raise_for_status()
            payload = res.json()
    except Exception:  # noqa: BLE001
        logger.info("admin: OpenPanel unreachable — section degrades to empty", exc_info=True)
        return {"enabled": False, "reason": "unavailable"}

    if not isinstance(payload, dict):
        return {"enabled": False, "reason": "unavailable"}
    # Our marker wins: an upstream body carrying its own `enabled` must not override it.
    return {**payload, "enabled": True}
