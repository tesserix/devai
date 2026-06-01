"""ObservabilityAdapter — the one ABC every monitoring backend implements.

Per the DevAI adapter pattern (CLAUDE.md §6): one ABC per family, lazy SDK
imports inside providers, a mandatory Noop, and a factory that never raises.
The family gives SRE agents a single, vendor-neutral way to pull metrics,
logs, and alerts from Datadog / New Relic / CloudWatch / Azure Monitor /
Prometheus / Elasticsearch / Grafana — configured in DevAI, consumed in SRE.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MetricSeries:
    """One metric time series. ``points`` are (unix_ts, value) pairs."""

    name: str
    provider: str
    points: list[tuple[float, float]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    unit: str = ""

    def latest(self) -> float | None:
        return self.points[-1][1] if self.points else None


@dataclass(slots=True)
class LogEntry:
    timestamp: float
    message: str
    provider: str
    level: str = ""
    service: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Alert:
    title: str
    provider: str
    severity: str = ""  # critical | high | warning | info
    state: str = ""  # firing | ok | no_data
    service: str = ""
    description: str = ""
    started_at: float | None = None


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    ok: bool
    detail: str = ""


class ObservabilityAdapter(ABC):
    """Vendor-neutral read surface over one observability backend.

    All methods are read-only. Implementations must never raise out of the
    family — on failure return an empty result and surface the reason via
    ``health_check``. ``provider_name`` is the lowercase provider key
    (e.g. "datadog").
    """

    provider_name: str = "base"

    @abstractmethod
    async def query_metrics(self, query: str, *, window_seconds: int = 3600) -> list[MetricSeries]:
        """Run a metrics query in the backend's native query language."""

    @abstractmethod
    async def query_logs(self, query: str, *, limit: int = 100, window_seconds: int = 3600) -> list[LogEntry]:
        """Search logs."""

    @abstractmethod
    async def get_alerts(self) -> list[Alert]:
        """Return currently active / firing alerts."""

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Is the backend reachable + authenticated?"""


__all__ = [
    "ObservabilityAdapter",
    "MetricSeries",
    "LogEntry",
    "Alert",
    "ProviderHealth",
]
