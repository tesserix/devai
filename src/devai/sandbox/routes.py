"""FastAPI routes for agent sandboxes — mounted at ``/api/sandboxes``.

Ownership mirrors the preview routes: a foreign sandbox reads as 404 rather than
403, and an anonymous caller (auth off) is pinned to a per-request owner so
anonymous sandboxes never cross-read one another.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request, Response, WebSocket
from pydantic import ValidationError

from devai.identity import Principal, extract_principal
from devai.sandbox.broker import BrokerError, CredentialBroker
from devai.sandbox.ide_proxy import proxy_ide_request, proxy_ide_socket
from devai.sandbox.job import sandbox_secret_name
from devai.sandbox.models import SandboxRecord, SandboxSpec
from devai.sandbox.service import SandboxError
from devai.sandbox.trace import TraceStore
from devai.sandbox.workspace_client import WorkspaceClient

if TYPE_CHECKING:
    from devai.sandbox.service import SandboxService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandboxes", tags=["sandbox"])


def _service(request: Request) -> SandboxService:
    svc: SandboxService | None = getattr(request.app.state, "sandbox_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="sandbox service unavailable")
    return svc


def _require_auth(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    return bool(getattr(config, "require_auth", False))


async def _resolve_principal(request: Request) -> Principal | None:
    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        return None


def _owner_handle(principal: Principal | None) -> str | None:
    if principal is None:
        return None
    if principal.tenant_id and principal.user_scope_id:
        return principal.user_scope_id
    for attr in ("email", "display_name", "uid"):
        val = getattr(principal, attr, None)
        if val:
            return str(val)
    return None


async def _write_scope(request: Request) -> tuple[str, bool]:
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None:
        if _require_auth(request):
            raise HTTPException(status_code=401, detail="authentication required")
        owner = f"anon:{uuid.uuid4().hex[:12]}"
    return owner, _is_admin(principal)


async def _read_scope(request: Request) -> tuple[str, bool]:
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None and _require_auth(request):
        raise HTTPException(status_code=401, detail="authentication required")
    return owner or "", _is_admin(principal)


def _is_admin(principal: Principal | None) -> bool:
    if principal is None:
        return False
    roles = set(principal.roles or [])
    return "platform-admin" in roles or ("admin" in roles and not principal.tenant_id)


def _view(record: SandboxRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


@router.post("", status_code=201)
async def create_sandbox(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    svc = _service(request)
    owner, _ = await _write_scope(request)
    try:
        spec = SandboxSpec.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False)) from e
    try:
        return _view(await svc.create(spec, owner=owner))
    except SandboxError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("")
async def list_sandboxes(request: Request, mine: bool = True) -> list[dict[str, Any]]:
    owner, is_admin = await _read_scope(request)
    records = await _service(request).list(owner=owner, is_admin=is_admin and not mine)
    return [_view(r) for r in records]


@router.get("/{sandbox_id}")
async def get_sandbox(request: Request, sandbox_id: str) -> dict[str, Any]:
    owner, is_admin = await _read_scope(request)
    record = await _service(request).get(sandbox_id, owner=owner, is_admin=is_admin)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    return _view(record)


@router.delete("/{sandbox_id}")
async def destroy_sandbox(request: Request, sandbox_id: str) -> dict[str, Any]:
    owner, is_admin = await _read_scope(request)
    try:
        await _service(request).destroy(sandbox_id, owner=owner, is_admin=is_admin)
    except SandboxError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"destroyed": sandbox_id}


SANDBOX_TOKEN_HEADER = "X-DevAI-Sandbox-Token"


async def _authorize_sandbox_token(runtime: Any, sandbox_id: str, supplied: str | None) -> None:
    """Let a sandbox speak for itself, using the token only it can read."""
    try:
        expected = await runtime.read_secret_key(sandbox_secret_name(sandbox_id), "capability_token")
    except Exception as e:  # noqa: BLE001 — no Secret means nothing can authenticate as this sandbox
        raise HTTPException(status_code=401, detail="sandbox token not recognised") from e
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="sandbox token not recognised")


def _broker(request: Request) -> CredentialBroker:
    broker: CredentialBroker | None = getattr(request.app.state, "credential_broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="no credential broker configured")
    return broker


@router.post("/{sandbox_id}/credentials")
async def mint_credential(
    request: Request,
    sandbox_id: str,
    body: dict[str, Any],
    x_devai_sandbox_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Hand a sandbox the narrowest credential that satisfies its request."""
    runtime = getattr(getattr(request.app.state, "pipeline_service", None), "k8s_runtime", None)
    if x_devai_sandbox_token and runtime is not None:
        await _authorize_sandbox_token(runtime, sandbox_id, x_devai_sandbox_token)
        record = await _service(request).get(sandbox_id, owner="", is_admin=True)
        if record is None:
            raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    else:
        record = await _sandbox_or_404(request, sandbox_id)

    try:
        grant, secret = await _broker(request).grant(
            record, kind=str(body.get("kind", "scm")), scope=str(body.get("scope", ""))
        )
    except BrokerError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"grant": grant.model_dump(mode="json"), "value": secret}


@router.get("/{sandbox_id}/credentials")
async def list_grants(request: Request, sandbox_id: str) -> list[dict[str, Any]]:
    await _sandbox_or_404(request, sandbox_id)
    return [g.model_dump(mode="json") for g in _broker(request).grants(sandbox_id)]


async def _workspace_client(request: Request, record: SandboxRecord) -> WorkspaceClient:
    """Resolve the workspace endpoint + capability token for one sandbox.

    The token is read from the workspace Secret at call time — it is never in
    the sandbox row, which the dashboard and the audit log both read.
    """
    endpoint = (record.detail.get("workspace") or {}).get("endpoint") if record.detail else None
    if not endpoint:
        raise HTTPException(status_code=409, detail="this sandbox has no workspace")
    runtime = getattr(getattr(request.app.state, "pipeline_service", None), "k8s_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="cluster runtime unavailable")
    try:
        token = await runtime.read_secret_key(f"devai-sandbox-ws-{record.id}", "token")
    except Exception as e:  # noqa: BLE001 — a missing Secret means the workspace is gone
        raise HTTPException(status_code=409, detail="workspace is not ready") from e
    return WorkspaceClient(endpoint, token=token)


async def _sandbox_or_404(request: Request, sandbox_id: str) -> SandboxRecord:
    owner, is_admin = await _read_scope(request)
    record = await _service(request).get(sandbox_id, owner=owner, is_admin=is_admin)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    return record


# Previewed markup is written by the agent, and it is served from the API's own
# origin — `sandbox` drops it into an opaque origin so it cannot script the
# dashboard or read its session.
_PREVIEW_HEADERS = {
    "Content-Security-Policy": "sandbox allow-forms allow-popups; default-src 'self' 'unsafe-inline' data:",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Cache-Control": "no-store",
}


@router.get("/{sandbox_id}/preview")
async def preview_ports(request: Request, sandbox_id: str) -> dict[str, Any]:
    record = await _sandbox_or_404(request, sandbox_id)
    client = await _workspace_client(request, record)
    return {"ports": await client.preview_ports()}


@router.get("/{sandbox_id}/preview/{port}/{path:path}")
async def preview(request: Request, sandbox_id: str, port: int, path: str = "") -> Response:
    """Serve what the run is hosting, through the same ownership check as its files."""
    record = await _sandbox_or_404(request, sandbox_id)
    client = await _workspace_client(request, record)
    await _service(request).touch(sandbox_id)
    result = await client.preview_fetch(port, path)
    return Response(
        content=str(result.get("body", "")),
        status_code=int(result.get("status", 200)),
        media_type=str(result.get("content_type") or "text/plain"),
        headers=_PREVIEW_HEADERS,
    )


def _ide_endpoint(record: SandboxRecord) -> str:
    if not record.spec.ide:
        raise HTTPException(status_code=409, detail="this sandbox was not created with an editor")
    endpoint = (record.detail.get("workspace") or {}).get("endpoint") if record.detail else None
    if not endpoint:
        raise HTTPException(status_code=409, detail="this sandbox has no workspace")
    return str(endpoint)


@router.api_route("/{sandbox_id}/ide/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def ide(request: Request, sandbox_id: str, path: str = "") -> Response:
    """Take over the workspace in an editor, behind the sandbox's own ownership check."""
    record = await _sandbox_or_404(request, sandbox_id)
    endpoint = _ide_endpoint(record)
    await _service(request).touch(sandbox_id)
    return await proxy_ide_request(request, endpoint, path)


def _same_origin(websocket: WebSocket) -> bool:
    """A socket handshake carries the session cookie and no CORS check guards
    it, so a foreign Origin is another site driving this workspace. An absent
    Origin is a non-browser client, which has no ambient session to borrow."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    host = websocket.headers.get("host", "")
    return urlsplit(origin).netloc == host


@router.websocket("/{sandbox_id}/ide/{path:path}")
async def ide_ws(websocket: WebSocket, sandbox_id: str, path: str = "") -> None:
    """The editor's own socket — without it code-server renders and then hangs.

    Ownership is resolved the same way as on the HTTP side; a caller who cannot
    read the sandbox gets the socket closed rather than a 404 body.
    """
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    owner, is_admin = await _read_scope(websocket)  # type: ignore[arg-type]
    record = await _service(websocket).get(sandbox_id, owner=owner, is_admin=is_admin)  # type: ignore[arg-type]
    if record is None or not record.spec.ide:
        await websocket.close(code=1008)
        return
    endpoint = (record.detail.get("workspace") or {}).get("endpoint") if record.detail else None
    if not endpoint:
        await websocket.close(code=1008)
        return
    await proxy_ide_socket(websocket, str(endpoint), path)


@router.post("/{sandbox_id}/shell")
async def workspace_exec(request: Request, sandbox_id: str, body: dict[str, Any]) -> dict[str, Any]:
    record = await _sandbox_or_404(request, sandbox_id)
    client = await _workspace_client(request, record)
    await _service(request).touch(sandbox_id)
    return await client.exec(str(body.get("command", "")), timeout=float(body.get("timeout", 120.0)))


@router.post("/{sandbox_id}/files/{operation}")
async def workspace_files(request: Request, sandbox_id: str, operation: str, body: dict[str, Any]) -> dict[str, Any]:
    record = await _sandbox_or_404(request, sandbox_id)
    client = await _workspace_client(request, record)
    await _service(request).touch(sandbox_id)
    if operation == "read":
        return {"content": await client.read(str(body.get("path", "")))}
    if operation == "write":
        return await client.write(str(body.get("path", "")), str(body.get("content", "")))
    if operation == "list":
        return {"entries": await client.list(str(body.get("path", ".")))}
    if operation == "search":
        return {"hits": await client.search(str(body.get("needle", "")), str(body.get("path", ".")))}
    raise HTTPException(status_code=404, detail=f"unknown file operation {operation!r}")


def _invoker(request: Request) -> Any:
    inv = getattr(request.app.state, "sandbox_invoker", None)
    if inv is None:
        raise HTTPException(status_code=503, detail="sandbox invoker unavailable")
    return inv


def _traces(request: Request) -> TraceStore:
    store: TraceStore | None = getattr(request.app.state, "sandbox_traces", None)
    if store is None:
        raise HTTPException(status_code=503, detail="sandbox traces unavailable")
    return store


@router.post("/{sandbox_id}/invoke")
async def invoke_sandbox(request: Request, sandbox_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """One turn of the pinned agent, answering with the trace it produced."""
    owner, is_admin = await _read_scope(request)
    record = await _service(request).get(sandbox_id, owner=owner, is_admin=is_admin)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    invoker = _invoker(request)
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")
    await _service(request).touch(sandbox_id)
    try:
        invocation = await invoker.invoke(record, message=message, triggered_by=owner or record.owner)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return invocation.to_dict()


def _evals(request: Request) -> Any:
    runner = getattr(request.app.state, "sandbox_evals", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="sandbox checks unavailable")
    return runner


@router.post("/{sandbox_id}/evals")
async def run_evals(request: Request, sandbox_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run the saved checks against this sandbox and answer with the scorecard."""
    from devai.sandbox.evals import EvalCase

    owner, is_admin = await _read_scope(request)
    record = await _service(request).get(sandbox_id, owner=owner, is_admin=is_admin)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    runner = _evals(request)
    try:
        cases = [EvalCase.model_validate(c) for c in body.get("cases") or []]
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"invalid case: {e.errors()[0]['msg']}") from e
    await _service(request).touch(sandbox_id)
    try:
        run = await runner.run(record, cases, triggered_by=owner or record.owner)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return run.to_dict()


@router.get("/{sandbox_id}/evals")
async def list_evals(request: Request, sandbox_id: str, limit: int = 20) -> list[dict[str, Any]]:
    await _sandbox_or_404(request, sandbox_id)
    found = await _evals(request).store.list_for_sandbox(sandbox_id, limit=limit)
    return [run.to_dict() for run in found]


@router.get("/{sandbox_id}/evals/{run_id}")
async def get_eval_run(request: Request, sandbox_id: str, run_id: str) -> dict[str, Any]:
    await _sandbox_or_404(request, sandbox_id)
    run = await _evals(request).store.get(sandbox_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"check run {run_id!r} not found")
    return run.to_dict()


@router.get("/{sandbox_id}/traces")
async def list_traces(request: Request, sandbox_id: str, limit: int = 50) -> list[dict[str, Any]]:
    await _sandbox_or_404(request, sandbox_id)
    found = await _traces(request).list_for_sandbox(sandbox_id, limit=limit)
    return [inv.to_dict() for inv in found]


@router.get("/{sandbox_id}/traces/{invocation_id}")
async def get_trace(request: Request, sandbox_id: str, invocation_id: str) -> dict[str, Any]:
    await _sandbox_or_404(request, sandbox_id)
    invocation = await _traces(request).get(sandbox_id, invocation_id)
    if invocation is None:
        raise HTTPException(status_code=404, detail=f"trace {invocation_id!r} not found")
    return invocation.to_dict()


__all__ = ["router"]
