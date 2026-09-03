"""Factory — pick the telemetry backend at runtime.

Reads `settings.telemetry_provider` (env: `DEVAI_TELEMETRY_PROVIDER`) and
returns one of:

    noop  → NoopTelemetryAdapter   (discards; the default)
    otel  → OtelTelemetryAdapter   (OTLP/HTTP to the collector)

Graceful degradation (identical rules to every other family):

  - `DEVAI_METRICS_ENABLED=false`        → Noop (telemetry globally off)
  - provider unset / "noop"              → Noop
  - provider="otel" but no endpoint      → Noop + warning (AdapterNotConfigured)
  - provider="otel" but exporter missing → Noop + warning (AdapterNotInstalled)
  - unknown provider                     → Noop + warning

The factory NEVER raises — the pod must degrade, not crash. Telemetry is the
last thing that should be allowed to take down a request path.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import (
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)
from devai.adapters.telemetry.base import TelemetryAdapter
from devai.adapters.telemetry.noop import NoopTelemetryAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "otel", "langfuse")


def _build_noop(settings: Any) -> TelemetryAdapter:  # noqa: ARG001
    return NoopTelemetryAdapter()


def _build_otel(settings: Any) -> TelemetryAdapter:
    from devai.adapters.telemetry.otel import OtelTelemetryAdapter

    endpoint = getattr(settings, "otel_endpoint", "") or ""
    if not endpoint:
        raise AdapterNotConfigured("otel telemetry adapter requires DEVAI_OTEL_ENDPOINT")
    return OtelTelemetryAdapter(
        endpoint=endpoint,
        service_name=getattr(settings, "otel_service_name", "") or "devai",
        service_namespace=getattr(settings, "otel_service_namespace", "") or "devai",
        deployment_environment=getattr(settings, "otel_deployment_environment", "") or "prod",
        export_interval_ms=int(getattr(settings, "otel_export_interval_ms", 15000) or 15000),
    )


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "")


def _build_langfuse(settings: Any) -> TelemetryAdapter:
    from devai.adapters.telemetry.langfuse import LangfuseTelemetryAdapter

    return LangfuseTelemetryAdapter(
        base_url=getattr(settings, "langfuse_base_url", "") or "",
        public_key=_secret(getattr(settings, "langfuse_public_key", "")),
        secret_key=_secret(getattr(settings, "langfuse_secret_key", "")),
        service_name=getattr(settings, "otel_service_name", "") or "devai",
        service_namespace=getattr(settings, "otel_service_namespace", "") or "devai",
        deployment_environment=getattr(settings, "otel_deployment_environment", "") or "prod",
        export_interval_ms=int(getattr(settings, "otel_export_interval_ms", 15000) or 15000),
        # Langfuse takes traces only; metrics still go to the cluster collector.
        metrics_endpoint=getattr(settings, "otel_endpoint", "") or "",
    )


telemetry_registry: AdapterRegistry[TelemetryAdapter] = AdapterRegistry("telemetry")
telemetry_registry.register("noop", _build_noop)
telemetry_registry.register("otel", _build_otel)
telemetry_registry.register("langfuse", _build_langfuse)


def create_telemetry_adapter(settings: Any) -> TelemetryAdapter:
    """Resolve `settings.telemetry_provider` to a constructed adapter.

    Never raises. On any error returns a NoopTelemetryAdapter and logs why.
    """
    if not bool(getattr(settings, "metrics_enabled", True)):
        logger.info("metrics disabled (DEVAI_METRICS_ENABLED=false) — telemetry Noop")
        return NoopTelemetryAdapter()

    provider = (getattr(settings, "telemetry_provider", "noop") or "noop").lower()
    if not telemetry_registry.has(provider):
        logger.warning(
            "telemetry_provider=%r is unknown (known: %s) — using Noop",
            provider,
            ", ".join(telemetry_registry.known()),
        )
        return NoopTelemetryAdapter()

    try:
        adapter = telemetry_registry.resolve(provider, settings)
        logger.info("TelemetryAdapter active: %s", adapter.provider_name)
        return adapter
    except AdapterNotInstalled as e:
        logger.warning("telemetry_provider=%s: %s — using Noop", provider, e)
        return NoopTelemetryAdapter()
    except AdapterNotConfigured as e:
        logger.warning("telemetry_provider=%s: %s — using Noop", provider, e)
        return NoopTelemetryAdapter()
    except AdapterError as e:
        logger.warning("telemetry_provider=%s failed to build (%s) — using Noop", provider, e)
        return NoopTelemetryAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("telemetry_provider=%s crashed during build — using Noop", provider)
        return NoopTelemetryAdapter()


__all__ = [
    "KNOWN_PROVIDERS",
    "create_telemetry_adapter",
    "telemetry_registry",
]
