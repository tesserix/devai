"""Sandbox credentials are explicit, user-owned, and never inherited."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse, LLMUsage
from devai.adapters.secrets.base import SecretRef, SecretsAdapter
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.credentials import (
    SandboxBudgetExceeded,
    SandboxCredentialError,
    SandboxCredentialResolver,
    SandboxMeteredLLMAdapter,
)
from devai.sandbox.models import (
    AgentRef,
    ModelRef,
    SandboxCredentials,
    SandboxRecord,
    SandboxSpec,
    SandboxStatus,
)
from devai.settings.models import Scope
from devai.settings.service import SettingsService


class _Secrets(SecretsAdapter):
    provider_name = "test"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def can_write(self) -> bool:
        return True

    async def set_secret(self, key, value, *, labels=None):
        del labels
        self.values[key] = value
        return SecretRef(name=key, provider=self.provider_name)

    async def get_secret(self, ref):
        name = ref.name if isinstance(ref, SecretRef) else str(ref)
        return self.values.get(name)

    async def delete_secret(self, ref):
        name = ref.name if isinstance(ref, SecretRef) else str(ref)
        return self.values.pop(name, None) is not None


class _LLM(LLMAdapter):
    provider_name = "sandbox-test"

    async def generate(self, request):  # type: ignore[override]
        return LLMResponse(text="ok")


def _record(
    connector: str = "sandbox-evals",
    *,
    confirmed: bool = True,
    provider: str = "anthropic",
    owner: str = "alice@example.com",
) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner=owner,
        spec=SandboxSpec(
            agent=AgentRef(name="agent", version="v1"),
            model=ModelRef(provider=provider, model="claude-sonnet-4-6"),
            credentials=SandboxCredentials(llm_connector=connector, confirmed=confirmed),
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_exact_user_connector_is_the_only_llm_credential_granted() -> None:
    secrets = _Secrets()
    service = SettingsService(secrets=secrets)
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        prefs={"claude_model": "claude-sonnet-4-6"},
        secret_values={"anthropic_api_key": "alice-sandbox-key"},
        updated_by="alice@example.com",
    )
    await service.upsert_connector(
        scope=Scope.TENANT,
        scope_id="tenant-a",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="openai",
        secret_values={"openai_api_key": "shared-tenant-key"},
        updated_by="tenant-admin@example.com",
    )
    built = []
    audit = []

    def build(settings):
        built.append(settings)
        return _LLM()

    async def record(event):
        audit.append(event)

    base = Settings(
        anthropic_api_key="platform-anthropic",
        openai_api_key="platform-openai",
        database_url="postgresql://platform-secret@db/devai",
        agentgateway_url="http://agentgateway.agentgateway-system.svc.cluster.local:9092",
        llm_gateway_required=True,
        llm_gateway_base_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080",
    )
    deps = StageDeps(
        config=base,
        llm=_LLM(),
        scm=object(),  # type: ignore[arg-type]
        llm_resolver=object(),
        scm_resolver=object(),
        settings_service=service,
        secrets=secrets,
        extra={"production": object()},
    )
    resolver = SandboxCredentialResolver(service=service, adapter_factory=build, audit=record)

    isolated = await resolver.resolve(_record(), deps)

    assert isolated.llm is not deps.llm
    assert isolated.scm is None
    assert isolated.llm_resolver is None
    assert isolated.scm_resolver is None
    assert isolated.settings_service is None
    assert isolated.secrets is None
    assert isolated.extra is None
    assert built[0].anthropic_api_key == "alice-sandbox-key"
    assert built[0].openai_api_key == ""
    assert built[0].database_url == Settings.model_construct().database_url
    assert built[0].agentgateway_url == base.agentgateway_url
    assert built[0].llm_gateway_required is True
    assert built[0].llm_gateway_base_url == base.llm_gateway_base_url
    assert audit == [
        {
            "action": "sandbox.credential.grant",
            "sandbox_id": "sb-1",
            "owner": "alice@example.com",
            "kind": "llm",
            "connector": "sandbox-evals",
            "provider": "anthropic",
        }
    ]
    assert "alice-sandbox-key" not in str(audit)
    assert "shared-tenant-key" not in repr(built[0])


@pytest.mark.asyncio
async def test_sandbox_llm_connectors_always_require_agentgateway_routing() -> None:
    secrets = _Secrets()
    service = SettingsService(secrets=secrets)
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "alice-sandbox-key"},
        updated_by="alice@example.com",
    )
    built = []

    def build(settings):
        built.append(settings)
        return _LLM()

    base = Settings(
        llm_gateway_required=False,
        llm_gateway_base_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080",
    )

    await SandboxCredentialResolver(service=service, adapter_factory=build).resolve(
        _record(),
        StageDeps(config=base),
    )

    assert built[0].llm_gateway_required is True


@pytest.mark.asyncio
async def test_missing_agentgateway_route_fails_closed_instead_of_calling_a_provider_directly() -> None:
    secrets = _Secrets()
    service = SettingsService(secrets=secrets)
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "alice-sandbox-key"},
        updated_by="alice@example.com",
    )

    with pytest.raises(SandboxCredentialError, match="incomplete"):
        await SandboxCredentialResolver(service=service).resolve(
            _record(),
            StageDeps(config=Settings(llm_gateway_required=False, llm_gateway_base_url="")),
        )


@pytest.mark.asyncio
async def test_shared_or_another_users_connector_never_satisfies_a_sandbox_grant() -> None:
    service = SettingsService(secrets=_Secrets())
    await service.upsert_connector(
        scope=Scope.TENANT,
        scope_id="tenant-a",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "shared-key"},
    )
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="bob@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "bob-key"},
    )
    resolver = SandboxCredentialResolver(service=service, adapter_factory=lambda settings: _LLM())

    with pytest.raises(SandboxCredentialError, match="alice@example.com"):
        await resolver.resolve(_record(), StageDeps(config=Settings()))


@pytest.mark.asyncio
async def test_same_connector_name_is_resolved_only_in_the_qualified_tenant_user_scope() -> None:
    secrets = _Secrets()
    service = SettingsService(secrets=secrets)
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="tenant-a:subject-1",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "tenant-a-key"},
    )
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="tenant-b:subject-1",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="anthropic",
        secret_values={"anthropic_api_key": "tenant-b-key"},
    )
    built = []

    def build(settings):
        built.append(settings)
        return _LLM()

    await SandboxCredentialResolver(service=service, adapter_factory=build).resolve(
        _record(owner="tenant-a:subject-1"),
        StageDeps(config=Settings(llm_gateway_base_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080")),
    )

    assert built[0].anthropic_api_key == "tenant-a-key"
    assert "tenant-b-key" not in repr(built[0])


@pytest.mark.asyncio
async def test_connector_access_has_to_be_explicit_in_the_sandbox_spec() -> None:
    resolver = SandboxCredentialResolver(service=SettingsService(secrets=_Secrets()))

    with pytest.raises(SandboxCredentialError, match="explicitly granted"):
        await resolver.resolve(_record(connector="", confirmed=False), StageDeps(config=Settings()))


@pytest.mark.asyncio
async def test_connector_access_has_to_be_confirmed_in_the_sandbox_spec() -> None:
    resolver = SandboxCredentialResolver(service=SettingsService(secrets=_Secrets()))
    record = _record()
    corrupt_credentials = SandboxCredentials.model_construct(llm_connector="sandbox-evals", confirmed=False)
    record = record.model_copy(update={"spec": record.spec.model_copy(update={"credentials": corrupt_credentials})})

    with pytest.raises(SandboxCredentialError, match="confirm"):
        await resolver.resolve(record, StageDeps(config=Settings()))


@pytest.mark.asyncio
async def test_keyless_vertex_cannot_borrow_the_api_workload_identity() -> None:
    service = SettingsService(secrets=_Secrets())
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="vertex_gemini",
        prefs={"vertex_project": "alice-project"},
        updated_by="alice@example.com",
    )
    resolver = SandboxCredentialResolver(service=service, adapter_factory=lambda settings: _LLM())

    with pytest.raises(SandboxCredentialError, match="own credential"):
        await resolver.resolve(_record(provider="vertex_gemini"), StageDeps(config=Settings()))


@pytest.mark.asyncio
async def test_connector_provider_has_to_match_the_pinned_sandbox_model() -> None:
    service = SettingsService(secrets=_Secrets())
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider="openai",
        secret_values={"openai_api_key": "alice-openai-key"},
        updated_by="alice@example.com",
    )
    resolver = SandboxCredentialResolver(service=service, adapter_factory=lambda settings: _LLM())

    with pytest.raises(SandboxCredentialError, match="does not match"):
        await resolver.resolve(_record(), StageDeps(config=Settings()))


@pytest.mark.parametrize(
    ("model_provider", "connector_provider", "secret_name"),
    [
        ("claude", "anthropic", "anthropic_api_key"),
        ("codex", "openai", "openai_api_key"),
        ("gemini", "vertex_gemini", "vertex_api_key"),
        ("vertex", "vertex_gemini", "vertex_api_key"),
        ("google", "vertex_gemini", "vertex_api_key"),
    ],
)
@pytest.mark.asyncio
async def test_model_provider_aliases_match_their_user_connector(
    model_provider: str,
    connector_provider: str,
    secret_name: str,
) -> None:
    service = SettingsService(secrets=_Secrets())
    await service.upsert_connector(
        scope=Scope.USER,
        scope_id="alice@example.com",
        connector_key="llm",
        instance_id="sandbox-evals",
        provider=connector_provider,
        secret_values={secret_name: "alice-key"},
        updated_by="alice@example.com",
    )
    resolver = SandboxCredentialResolver(service=service, adapter_factory=lambda settings: _LLM())

    isolated = await resolver.resolve(_record(provider=model_provider), StageDeps(config=Settings()))

    assert isinstance(isolated.llm, SandboxMeteredLLMAdapter)


@pytest.mark.asyncio
async def test_sandbox_llm_calls_are_attributed_and_bounded_before_dispatch() -> None:
    class _Capture(LLMAdapter):
        provider_name = "anthropic"
        default_model = "claude-sonnet-4-6"

        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def generate(self, request):  # type: ignore[override]
            self.requests.append(request)
            return LLMResponse(
                text="ok",
                model="claude-sonnet-4-6",
                usage=LLMUsage(prompt_tokens=4, completion_tokens=6, total_tokens=10),
            )

    inner = _Capture()
    metered = SandboxMeteredLLMAdapter(
        inner,
        sandbox_id="sb-1",
        owner="alice@example.com",
        max_tokens=12,
        max_cost_usd=1.0,
        estimate_cost=lambda provider, model, prompt, completion: 0.25,
    )

    response = await metered.generate(LLMRequest(max_tokens=100))

    assert inner.requests[0].max_tokens == 12
    assert inner.requests[0].extra == {
        "sandbox_id": "sb-1",
        "run_id": "sandbox:sb-1",
        "user_id": "alice@example.com",
    }
    assert response.extra["sandbox_tokens_used"] == 10
    assert response.extra["sandbox_cost_usd"] == 0.25

    with pytest.raises(SandboxBudgetExceeded, match="token budget"):
        await metered.generate(LLMRequest())


@pytest.mark.asyncio
async def test_sandbox_cost_budget_stops_the_run_and_names_the_sandbox() -> None:
    metered = SandboxMeteredLLMAdapter(
        _LLM(),
        sandbox_id="sb-cost",
        owner="alice@example.com",
        max_tokens=100,
        max_cost_usd=0.1,
        estimate_cost=lambda provider, model, prompt, completion: 0.2,
    )

    with pytest.raises(SandboxBudgetExceeded, match="sb-cost.*cost budget"):
        await metered.generate(LLMRequest())
