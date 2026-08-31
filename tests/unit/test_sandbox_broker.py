"""The credential broker (#205).

The principle being tested: a sandbox holds nothing worth stealing. It never
carries a standing secret — it asks for a scoped, expiring one, every grant is
recorded, and teardown ends them all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from devai.sandbox.broker import MAX_GRANT_TTL_S, BrokerError, CredentialBroker
from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus


def _record(sandbox_id: str = "sb1", *, ttl_seconds: int = 3600, scopes: list[str] | None = None) -> SandboxRecord:
    now = datetime.now(UTC)
    if scopes is None:
        scopes = ["tesserix/devai", "r", "a", "b"]
    return SandboxRecord(
        id=sandbox_id,
        owner="dev@example.com",
        spec=SandboxSpec(
            agent=AgentRef(name="reviewer", version="1"),
            model=ModelRef(provider="anthropic", model="claude-sonnet-5"),
            ttl_seconds=ttl_seconds,
            allow_scopes=scopes,
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


class _FakeSource:
    kind = "scm"

    def __init__(self) -> None:
        self.minted: list[tuple[str, int]] = []
        self.revoked: list[str] = []

    async def mint(self, scope: str, *, ttl_seconds: int) -> tuple[str, datetime]:
        self.minted.append((scope, ttl_seconds))
        return f"tok-for-{scope}", datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    async def revoke(self, secret: str) -> None:
        self.revoked.append(secret)


def _broker(source: _FakeSource | None = None, audit: list | None = None) -> CredentialBroker:
    sink = None
    if audit is not None:

        async def sink(entry):  # noqa: ANN001, ANN202 — test double
            audit.append(entry)

    return CredentialBroker({"scm": source} if source else {}, audit=sink)


@pytest.mark.asyncio
async def test_a_grant_returns_a_credential_scoped_to_what_was_asked_for():
    source = _FakeSource()
    _, secret = await _broker(source).grant(_record(), kind="scm", scope="tesserix/devai")

    assert secret == "tok-for-tesserix/devai"
    assert source.minted[0][0] == "tesserix/devai"


@pytest.mark.asyncio
async def test_the_grant_record_never_carries_the_secret():
    source = _FakeSource()
    grant, secret = await _broker(source).grant(_record(), kind="scm", scope="tesserix/devai")

    assert secret not in grant.model_dump_json()


@pytest.mark.asyncio
async def test_a_grant_expires_well_inside_the_sandboxs_own_life():
    source = _FakeSource()
    await _broker(source).grant(_record(ttl_seconds=3600), kind="scm", scope="r")

    assert source.minted[0][1] <= MAX_GRANT_TTL_S


@pytest.mark.asyncio
async def test_a_grant_cannot_outlive_the_sandbox_that_asked_for_it():
    source = _FakeSource()
    await _broker(source).grant(_record(ttl_seconds=120), kind="scm", scope="r")

    assert source.minted[0][1] <= 120


@pytest.mark.asyncio
async def test_an_unconfigured_kind_says_so_instead_of_falling_back_to_a_platform_key():
    with pytest.raises(BrokerError, match="no credential source"):
        await _broker().grant(_record(), kind="scm", scope="r")


@pytest.mark.asyncio
async def test_every_grant_is_recorded_with_who_what_and_when():
    log: list = []
    grant, _ = await _broker(_FakeSource(), audit=log).grant(_record("sb9"), kind="scm", scope="tesserix/devai")

    assert log == [grant]
    assert grant.sandbox_id == "sb9"
    assert grant.granted_to == "dev@example.com"
    assert grant.scope == "tesserix/devai"
    assert grant.expires_at > grant.created_at


@pytest.mark.asyncio
async def test_grants_are_listed_per_sandbox_so_an_audit_can_answer_what_it_held():
    broker = _broker(_FakeSource())
    await broker.grant(_record("sb1"), kind="scm", scope="a")
    await broker.grant(_record("sb2"), kind="scm", scope="b")

    assert [g.scope for g in broker.grants("sb1")] == ["a"]


@pytest.mark.asyncio
async def test_teardown_revokes_every_credential_the_sandbox_was_given():
    source = _FakeSource()
    broker = _broker(source)
    _, secret = await broker.grant(_record("sb1"), kind="scm", scope="a")

    await broker.revoke_all("sb1")

    assert source.revoked == [secret]
    assert broker.grants("sb1") == []


@pytest.mark.asyncio
async def test_a_revoked_grant_is_recorded_as_revoked_not_forgotten():
    log: list = []
    broker = _broker(_FakeSource(), audit=log)
    await broker.grant(_record("sb1"), kind="scm", scope="a")

    await broker.revoke_all("sb1")

    assert log[-1].revoked_at is not None


@pytest.mark.asyncio
async def test_a_source_that_fails_to_revoke_does_not_block_teardown():
    class _Stubborn(_FakeSource):
        async def revoke(self, secret: str) -> None:
            raise RuntimeError("upstream down")

    broker = _broker(_Stubborn())
    await broker.grant(_record("sb1"), kind="scm", scope="a")

    await broker.revoke_all("sb1")

    assert broker.grants("sb1") == []


# ── the GitHub source ─────────────────────────────────────────────────


class _FakeHTTP:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, dict]] = []

    async def post(self, path: str, *, json=None, headers=None):  # noqa: ANN001, ANN202
        self.posts.append((path, json or {}))
        return _Resp(self.payload)

    async def delete(self, path: str, *, headers=None):  # noqa: ANN001, ANN202
        self.deletes.append((path, headers or {}))
        return _Resp({})


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 201

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _github_source(http: _FakeHTTP):  # noqa: ANN202
    from devai.sandbox.broker_github import GitHubScopedSource

    return GitHubScopedSource(app_id=1, private_key="k", installation_id=2, http=http, sign=lambda: "jwt")


@pytest.mark.asyncio
async def test_the_github_source_mints_a_token_for_one_repo_not_the_installation():
    http = _FakeHTTP({"token": "ghs_x", "expires_at": "2030-01-01T00:00:00Z"})

    secret, _ = await _github_source(http).mint("tesserix/devai", ttl_seconds=600)

    _path, body = http.posts[0]
    assert body["repositories"] == ["devai"]
    assert secret == "ghs_x"


@pytest.mark.asyncio
async def test_the_github_source_asks_for_contents_write_and_nothing_wider():
    http = _FakeHTTP({"token": "ghs_x"})

    await _github_source(http).mint("tesserix/devai", ttl_seconds=600)

    assert http.posts[0][1]["permissions"] == {"contents": "write", "pull_requests": "write"}


@pytest.mark.asyncio
async def test_revoking_a_github_grant_deletes_the_token_upstream():
    http = _FakeHTTP({"token": "ghs_x"})
    source = _github_source(http)

    await source.revoke("ghs_x")

    assert http.deletes[0][0] == "/installation/token"
    assert http.deletes[0][1]["Authorization"] == "token ghs_x"


# ── reaching the broker from inside a sandbox ─────────────────────────


class _FakeRuntime:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.secrets = secrets or {}
        self.config = type("C", (), {"namespace": "devai"})()

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict) -> None:
        self.applied.append(manifest)

    async def delete_manifest(self, kind: str, name: str, namespace: str | None = None) -> None:
        self.deleted.append((kind, name))

    async def read_secret_key(self, name: str, key: str, namespace: str | None = None) -> str:
        return self.secrets[f"{name}/{key}"]


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict | None = None) -> None:
        self.calls.append((sandbox_id, status, detail))


@pytest.mark.asyncio
async def test_provisioning_mints_the_sandboxs_own_capability_token():
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    result = await SandboxProvisioner(rt, _FakeStore()).provision(_record("sb1"))

    secret = next(m for m in rt.applied if m["kind"] == "Secret" and m["metadata"]["name"] == "devai-sandbox-sb1")
    assert secret["stringData"]["capability_token"]
    # It authenticates the sandbox, so it belongs nowhere the sandbox row is read.
    assert secret["stringData"]["capability_token"] not in str(result.detail)


def test_a_sandboxed_job_can_read_its_own_capability_token():
    from devai.sandbox.job import apply_sandbox_boundary

    job = {
        "metadata": {"name": "j"},
        "spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": "runner", "env": []}]}}},
    }
    fenced = apply_sandbox_boundary(job, _record("sb1"))
    entry = next(
        e for e in fenced["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "DEVAI_SANDBOX_TOKEN"
    )
    assert entry["valueFrom"]["secretKeyRef"] == {
        "name": "devai-sandbox-sb1",
        "key": "capability_token",
        "optional": True,
    }


@pytest.mark.asyncio
async def test_the_broker_route_refuses_a_caller_with_the_wrong_token():
    from fastapi import HTTPException

    from devai.sandbox.routes import _authorize_sandbox_token

    rt = _FakeRuntime({"devai-sandbox-sb1/capability_token": "right"})
    with pytest.raises(HTTPException) as e:
        await _authorize_sandbox_token(rt, _record("sb1"), "wrong")
    assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_the_broker_route_accepts_the_sandboxs_own_token():
    from devai.sandbox.routes import _authorize_sandbox_token

    rt = _FakeRuntime({"devai-sandbox-sb1/capability_token": "right"})
    await _authorize_sandbox_token(rt, _record("sb1"), "right")


@pytest.mark.asyncio
async def test_destroying_a_sandbox_revokes_its_credentials():
    from devai.sandbox.provisioner import SandboxProvisioner

    broker = _broker(_FakeSource())
    await broker.grant(_record("sb1"), kind="scm", scope="a")

    await SandboxProvisioner(_FakeRuntime(), _FakeStore(), broker=broker).teardown(_record("sb1"))

    assert broker.grants("sb1") == []


@pytest.mark.asyncio
async def test_a_sandbox_cannot_ask_for_a_scope_it_was_not_created_with():
    """The agent chooses the arguments, so the scope has to be bounded by the spec."""
    source = _FakeSource()
    record = _record()
    with pytest.raises(BrokerError, match="attacker/exfil"):
        await _broker(source).grant(record, kind="scm", scope="attacker/exfil")
    assert source.minted == []


@pytest.mark.asyncio
async def test_a_sandbox_with_no_declared_scope_can_obtain_nothing():
    source = _FakeSource()
    record = _record(scopes=[])
    with pytest.raises(BrokerError):
        await _broker(source).grant(record, kind="scm", scope="tesserix/devai")
    assert source.minted == []
