"""PrincipalLLMResolver — per-user LLM adapters for pipeline runs.

The chat gateway already applies the settings overlay per conversation; this
gives the PIPELINE the same power: a run triggered by a user whose Settings
configure an LLM connector executes against THAT user's provider/model/keys,
fully isolated from every other tenant's. Users with no LLM connector fall
through to the platform default adapter (``deps.llm``) — behavior-neutral.

Adapters are cached by override fingerprint (bounded), so N runs by the same
user share one client instead of re-dialing per stage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.settings.models import CONNECTOR_BY_KEY

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.identity import Principal
    from devai.settings.service import SettingsService

logger = logging.getLogger(__name__)

_CACHE_MAX = 64


def _llm_attrs() -> set[str]:
    """Settings attributes that affect LLM adapter construction — derived
    from the catalog so new connector fields are picked up automatically."""
    spec = CONNECTOR_BY_KEY.get("llm")
    if spec is None:  # pragma: no cover - catalog always has llm
        return set()
    attrs = {f.settings_attr for f in spec.fields}
    attrs.add(spec.provider_attr)
    return attrs


class PrincipalLLMResolver:
    """Resolve a Principal to their own LLM adapter, or ``None`` for default."""

    def __init__(self, base_settings: Any, service: SettingsService | None) -> None:
        self._base = base_settings
        self._service = service
        self._cache: dict[str, LLMAdapter] = {}
        self._platform: LLMAdapter | None = None

    def _platform_adapter(self) -> LLMAdapter | None:
        """Lazy platform-default adapter used as the resilience fallback
        behind per-user providers. Never raises."""
        if self._platform is None:
            try:
                from devai.adapters.llm.factory import create_llm_chain

                # The full platform chain (vertex → groq → openrouter →
                # anthropic in prod), so a user's broken provider degrades
                # through every configured backend before giving up.
                self._platform = create_llm_chain(self._base)
            except Exception:  # noqa: BLE001
                logger.warning("settings: platform fallback adapter build failed", exc_info=True)
                return None
        return self._platform

    async def resolve(self, principal: Principal | None) -> LLMAdapter | None:
        """The principal's adapter, or None when nothing user-specific applies.

        Never raises — any failure logs and returns None so the run proceeds
        on the platform default (same degradation contract as every factory).
        """
        if self._service is None or principal is None:
            return None
        try:
            from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

            overlay = await build_overlay(self._base, principal, self._service)
            if not isinstance(overlay, PrincipalSettingsOverlay):
                return None
            relevant = set(overlay.overlaid_attrs) & _llm_attrs()
            if not relevant:
                return None

            fingerprint = "|".join(f"{a}={getattr(overlay, a)!r}" for a in sorted(relevant))
            cached = self._cache.get(fingerprint)
            if cached is not None:
                return cached

            from devai.adapters.llm.factory import create_llm_adapter

            adapter = create_llm_adapter(overlay)
            if adapter.provider_name == "noop" and getattr(overlay, "llm_provider", "") != "noop":
                # The user's config is incomplete/broken — better to run on
                # the platform default than to silently answer with Noop.
                logger.warning(
                    "settings: per-user LLM for %s degraded to noop — using platform default",
                    getattr(principal, "email", "?"),
                )
                return None

            # The user's enabled-models policy (Settings toggles): requests
            # for a disabled model clamp to the connector's default.
            enabled = getattr(overlay, "llm_enabled_models", None) or []
            if enabled:
                from devai.adapters.llm.fallback import ModelAllowlistLLMAdapter

                adapter = ModelAllowlistLLMAdapter(adapter, list(enabled))

            # Runtime resilience: unless strict per-user isolation is on,
            # chain the platform default behind the user's provider so an
            # outage/misconfiguration degrades instead of failing their run.
            if not bool(getattr(self._base, "llm_require_user_connector", False)):
                platform = self._platform_adapter()
                if platform is not None and platform.provider_name not in ("noop", adapter.provider_name):
                    from devai.adapters.llm.fallback import FallbackLLMAdapter

                    adapter = FallbackLLMAdapter(adapter, platform)
            if len(self._cache) >= _CACHE_MAX:
                self._cache.clear()
            self._cache[fingerprint] = adapter
            logger.info(
                "settings: per-user LLM active for %s (provider=%s)",
                getattr(principal, "email", "?"),
                adapter.provider_name,
            )
            return adapter
        except Exception:  # noqa: BLE001
            logger.warning("settings: per-user LLM resolution failed — using default", exc_info=True)
            return None

    async def resolve_for_email(self, email: str) -> LLMAdapter | None:
        """Convenience for run records that only carry ``triggered_by``."""
        if not email or "@" not in email:
            return None
        from devai.identity import Principal

        return await self.resolve(Principal(uid="", email=email))

    async def settings_for_email(self, email: str) -> Any:
        """The user's settings overlay (or the base settings when they have
        nothing configured). Lets callers build adapters for a DIFFERENT
        provider than the user's default while still honoring their keys —
        e.g. a specialization that pins ``llm_provider`` per role."""
        if self._service is None or not email or "@" not in email:
            return self._base
        try:
            from devai.identity import Principal
            from devai.settings.overlay import build_overlay

            return await build_overlay(self._base, Principal(uid="", email=email), self._service)
        except Exception:  # noqa: BLE001
            logger.warning("settings: overlay fetch failed — using base settings", exc_info=True)
            return self._base


__all__ = ["PrincipalLLMResolver"]
