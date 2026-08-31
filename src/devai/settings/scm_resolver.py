"""PrincipalSCMResolver — per-user SCM clients for pipeline runs / agents.

The SCM counterpart to PrincipalLLMResolver: a run/agent triggered by a user
whose Settings configure a Source Control connector talks to git through THAT
user's own credentials — a PAT or their own GitHub App (JWT → installation
token) — instead of the platform's single global GitHub App. Users with no SCM
connector fall through to the platform client (``deps.scm``), behavior-neutral.

A user's resolved client is cached by override fingerprint (bounded) so N runs
by the same user share one client (and its installation-token cache) rather
than re-minting per stage. Cached clients are long-lived — callers must NOT
``close()`` them (the resolver owns their lifecycle); use ``settings_for_email``
instead if you need to build-and-close your own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.settings.models import CONNECTOR_BY_KEY

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.scm.base import SCMClient
    from devai.settings.service import SettingsService

logger = logging.getLogger(__name__)

_CACHE_MAX = 64


def _scm_attrs() -> set[str]:
    """Settings attributes that affect SCM client construction (catalog-derived,
    plus the inferred auth method the overlay sets)."""
    spec = CONNECTOR_BY_KEY.get("scm")
    if spec is None:  # pragma: no cover — catalog always has scm
        return {"scm_auth_method"}
    attrs = {f.settings_attr for f in spec.fields}
    attrs.add(spec.provider_attr)
    attrs.add("scm_auth_method")  # overlay infers this; not a connector field
    return attrs


class PrincipalSCMResolver:
    """Resolve a Principal to their own SCM client, or ``None`` for default."""

    def __init__(self, base_settings: Any, service: SettingsService | None) -> None:
        self._base = base_settings
        self._service = service
        self._cache: dict[str, SCMClient] = {}

    async def settings_for_email(self, email: str) -> Any:
        """The user's settings overlay (or base settings when nothing applies).

        For callers that want to build-and-close their own SCM client from the
        user's creds (e.g. chat tools) without touching the resolver's cache.
        """
        from devai.identity import Principal

        return await self.settings_for_principal(Principal(uid="", email=email))

    async def settings_for_principal(self, principal: Principal | None) -> Any:
        if self._service is None or principal is None:
            return self._base
        try:
            from devai.settings.overlay import build_overlay

            return await build_overlay(self._base, principal, self._service)
        except Exception:  # noqa: BLE001
            logger.warning("settings: SCM overlay fetch failed — using base settings", exc_info=True)
            return self._base

    def _has_own_scm(self, overlay: Any) -> bool:
        from devai.settings.overlay import PrincipalSettingsOverlay

        if not isinstance(overlay, PrincipalSettingsOverlay):
            return False
        return bool(set(overlay.overlaid_attrs) & _scm_attrs())

    async def resolve_for_email(self, email: str) -> SCMClient | None:
        """The user's own SCM client, or None when nothing user-specific applies.

        Never raises — any failure logs and returns None so the run proceeds on
        the platform client (same degradation contract as the LLM resolver).
        Cached by fingerprint; callers must NOT close the returned client.
        """
        if self._service is None or not email or "@" not in email:
            return None
        from devai.identity import Principal

        return await self.resolve(Principal(uid="", email=email))

    async def resolve(self, principal: Principal | None) -> SCMClient | None:
        pair = await self.resolve_with_overlay(principal)
        return pair[0] if pair is not None else None

    async def resolve_with_overlay(self, principal: Principal | None) -> tuple[SCMClient, Any] | None:
        """The user's own SCM client plus the settings overlay it came from.

        For callers that also need connector prefs (e.g. ``scm_organization``
        to scope a repo listing). Same contract as :meth:`resolve`: never
        raises, ``None`` when nothing user-specific applies.
        """
        if self._service is None or principal is None:
            return None
        try:
            overlay = await self.settings_for_principal(principal)
            if not self._has_own_scm(overlay):
                return None

            relevant = set(getattr(overlay, "overlaid_attrs", ()) or ()) & _scm_attrs()
            fingerprint = "|".join(f"{a}={getattr(overlay, a)!r}" for a in sorted(relevant))
            cached = self._cache.get(fingerprint)
            if cached is not None:
                return cached, overlay

            from devai.scm.factory import create_scm_client

            client = create_scm_client(overlay)
            if len(self._cache) >= _CACHE_MAX:
                # Drop the oldest-ish (clear all) — bounded; clients are cheap to
                # rebuild and the GitHub App token re-mints on first use.
                self._cache.clear()
            self._cache[fingerprint] = client
            logger.info(
                "settings: per-user SCM active for %s (provider=%s, auth=%s)",
                principal.email,
                getattr(overlay, "scm_provider", "?"),
                getattr(overlay, "scm_auth_method", "?"),
            )
            return client, overlay
        except Exception:  # noqa: BLE001
            logger.warning("settings: per-user SCM resolution failed — using platform client", exc_info=True)
            return None


__all__ = ["PrincipalSCMResolver"]
