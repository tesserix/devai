"""Credential boundary for sandbox invocations.

Sandbox code must never inherit the principal or platform dependency bundle.
The default resolver therefore fails closed until an explicitly granted,
sandbox-scoped LLM connector is available.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse

if TYPE_CHECKING:
    from devai.pipeline.interfaces import StageDeps
    from devai.sandbox.models import SandboxRecord
    from devai.settings.models import Connector, Scope
    from devai.settings.service import SettingsService

logger = logging.getLogger(__name__)

AuditSink = Callable[[dict[str, str]], Awaitable[None]]
AdapterFactory = Callable[[Any], LLMAdapter]
CostEstimator = Callable[[str, str, int, int], float]

_PROVIDER_SECRET = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "vertex_gemini": "vertex_api_key",
    "groq": "groq_api_key",
    "openrouter": "openrouter_api_key",
    "gateway": "llm_gateway_api_key",
}


class SandboxCredentialError(RuntimeError):
    """A sandbox has no explicitly granted credential for this invocation."""


class SandboxBudgetExceeded(RuntimeError):
    """A sandbox LLM call crossed a declared token or cost ceiling."""


class SandboxMeteredLLMAdapter(LLMAdapter):
    """Attribute and enforce one sandbox invocation's LLM allowance."""

    def __init__(
        self,
        inner: LLMAdapter,
        *,
        sandbox_id: str,
        owner: str,
        max_tokens: int,
        max_cost_usd: float,
        estimate_cost: CostEstimator | None = None,
    ) -> None:
        self._inner = inner
        self._sandbox_id = sandbox_id
        self._owner = owner
        self._max_tokens = max_tokens
        self._max_cost_usd = max_cost_usd
        self._tokens = 0
        self._cost_usd = 0.0
        self._estimate_cost = estimate_cost or self._default_estimate
        self.provider_name = inner.provider_name
        self.default_model = inner.default_model

    @staticmethod
    def _default_estimate(provider: str, model: str, prompt: int, completion: int) -> float:
        from devai.analytics.pricing import estimate_cost

        return float(estimate_cost(provider, model, prompt, completion))

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if self._tokens >= self._max_tokens:
            raise SandboxBudgetExceeded(f"sandbox {self._sandbox_id} exceeded its token budget")
        if self._cost_usd >= self._max_cost_usd:
            raise SandboxBudgetExceeded(f"sandbox {self._sandbox_id} exceeded its cost budget")

        remaining = self._max_tokens - self._tokens
        requested = request.max_tokens
        max_tokens = min(requested, remaining) if requested is not None else remaining
        tenant_id, separator, user_id = self._owner.partition(":")
        if not separator or self._owner.startswith("anon:"):
            tenant_id, user_id = "", self._owner
        attributed = replace(
            request,
            max_tokens=max_tokens,
            extra={
                **dict(request.extra or {}),
                "sandbox_id": self._sandbox_id,
                "run_id": f"sandbox:{self._sandbox_id}",
                "user_id": user_id,
                "tenant_id": tenant_id,
            },
        )
        response = await self._inner.generate(attributed)
        usage = response.usage
        prompt = int(usage.prompt_tokens or 0)
        completion = int(usage.completion_tokens or 0)
        used = int(usage.total_tokens or prompt + completion)
        model = response.model or attributed.model or self.default_model
        cost = self._estimate_cost(self.provider_name, model, prompt, completion)
        self._tokens += used
        self._cost_usd += max(0.0, cost)
        response.extra.update(
            {
                "sandbox_id": self._sandbox_id,
                "sandbox_tokens_used": self._tokens,
                "sandbox_cost_usd": self._cost_usd,
            }
        )
        if self._tokens > self._max_tokens:
            raise SandboxBudgetExceeded(f"sandbox {self._sandbox_id} exceeded its token budget")
        if self._cost_usd > self._max_cost_usd:
            raise SandboxBudgetExceeded(f"sandbox {self._sandbox_id} exceeded its cost budget")
        return response

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        del texts, model
        raise SandboxCredentialError("sandbox embedding credentials were not granted")

    async def list_models(self) -> list[dict[str, str]]:
        return await self._inner.list_models()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()

    async def close(self) -> None:
        await self._inner.close()


class SandboxCredentialProvider(Protocol):
    async def resolve(self, record: SandboxRecord, deps: StageDeps) -> StageDeps: ...


class _OneConnectorService:
    """Settings overlay view containing exactly one user-owned connector."""

    def __init__(self, connector: Connector, service: SettingsService) -> None:
        self._connector = connector
        self._service = service

    async def list_connectors(self, scope: Scope, scope_id: str) -> list[Connector]:
        connector = self._connector
        if connector.scope == scope and connector.scope_id == scope_id:
            return [connector]
        return []

    async def list_user_connectors_by_email(self, email: str) -> list[Connector]:
        connector = self._connector
        if connector.updated_by == email or connector.scope_id == email:
            return [connector]
        return []

    async def resolve_secret(self, ref_name: str) -> str | None:
        return await self._service.resolve_secret(ref_name)


def _safe_base(settings: Any) -> Any:
    """Defaults plus the non-secret AgentGateway routing contract."""
    from devai.config import Settings

    defaults = Settings.model_construct()
    return defaults.model_copy(
        update={
            "agentgateway_url": getattr(settings, "agentgateway_url", "") or "",
            "llm_gateway_base_url": getattr(settings, "llm_gateway_base_url", "") or "",
            "llm_gateway_required": True,
            "llm_fallback_provider": "",
        }
    )


class SandboxCredentialResolver:
    """Build an isolated dependency bundle from one explicit user connector."""

    def __init__(
        self,
        *,
        service: SettingsService | None = None,
        adapter_factory: AdapterFactory | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._service = service
        self._adapter_factory = adapter_factory
        self._audit = audit

    async def resolve(self, record: SandboxRecord, deps: StageDeps) -> StageDeps:
        connector_id = record.spec.credentials.llm_connector
        if not connector_id:
            raise SandboxCredentialError(
                "sandbox LLM connector is not explicitly granted; select a sandbox-scoped connector"
            )
        if not record.spec.credentials.confirmed:
            raise SandboxCredentialError("confirm sandbox access to the selected LLM connector")
        connector = await self._connector(record.owner, connector_id)
        if connector is None:
            raise SandboxCredentialError(f"sandbox LLM connector {connector_id!r} is not owned by {record.owner!r}")
        from devai.adapters.llm.factory import resolve_spec_provider

        model_provider = resolve_spec_provider(record.spec.model.provider)
        if model_provider is None or connector.provider != model_provider:
            raise SandboxCredentialError(
                f"sandbox LLM connector provider {connector.provider!r} does not match "
                f"the pinned model provider {record.spec.model.provider!r}"
            )
        secret_field = _PROVIDER_SECRET.get(connector.provider)
        if secret_field is None or secret_field not in connector.secret_refs:
            raise SandboxCredentialError(f"sandbox LLM connector {connector_id!r} must contain the user own credential")

        from devai.identity import Principal
        from devai.settings.overlay import build_overlay

        overlay = await build_overlay(
            _safe_base(deps.config),
            Principal(email=record.owner),
            _OneConnectorService(connector, self._service),  # type: ignore[arg-type]
        )
        factory = self._adapter_factory
        if factory is None:
            from devai.adapters.llm.factory import create_llm_adapter

            factory = create_llm_adapter
        try:
            adapter = factory(overlay)
        except Exception as exc:
            raise SandboxCredentialError(f"sandbox LLM connector {connector_id!r} could not create an adapter") from exc
        if adapter.provider_name == "noop" and connector.provider != "noop":
            raise SandboxCredentialError(f"sandbox LLM connector {connector_id!r} is incomplete")

        adapter = SandboxMeteredLLMAdapter(
            adapter,
            sandbox_id=record.id,
            owner=record.owner,
            max_tokens=record.spec.limits.max_tokens,
            max_cost_usd=record.spec.limits.max_cost_usd,
        )
        await self._record_grant(record, connector)
        return replace(
            deps,
            config=overlay,
            scm=None,
            state_manager=None,
            event_bus=None,
            a2a_bus=None,
            memory=None,
            llm=adapter,
            event_bus_adapter=None,
            secrets=None,
            settings_service=None,
            llm_resolver=None,
            scm_resolver=None,
            extra=None,
        )

    async def _connector(self, owner: str, instance_id: str) -> Connector | None:
        service = self._service
        if service is None:
            return None
        from devai.settings.models import Scope

        candidates = await service.list_connectors(Scope.USER, owner)
        if hasattr(service, "list_user_connectors_by_email"):
            candidates += await service.list_user_connectors_by_email(owner)
        for connector in candidates:
            if (
                connector.scope is Scope.USER
                and connector.connector_key == "llm"
                and connector.instance_id == instance_id
                and connector.enabled
            ):
                return connector
        return None

    async def _record_grant(self, record: SandboxRecord, connector: Connector) -> None:
        event = {
            "action": "sandbox.credential.grant",
            "sandbox_id": record.id,
            "owner": record.owner,
            "kind": "llm",
            "connector": connector.instance_id,
            "provider": connector.provider,
        }
        if self._audit is None:
            logger.info("sandbox credential grant: %s", event)
            return
        try:
            await self._audit(event)
        except Exception:  # noqa: BLE001 -- logging remains the durable aggregation fallback
            logger.warning("sandbox credential audit sink failed: %s", event, exc_info=True)


__all__ = [
    "SandboxBudgetExceeded",
    "SandboxCredentialError",
    "SandboxCredentialProvider",
    "SandboxCredentialResolver",
    "SandboxMeteredLLMAdapter",
]
