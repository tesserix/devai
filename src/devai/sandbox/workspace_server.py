"""HTTP surface of a sandbox workspace, served inside the sandbox pod.

Every route requires the per-sandbox capability token. There is deliberately no
unauthenticated mode: the prior art this is modelled on leaves its shell and
desktop wide open when an env var is unset, and that is not a default anyone
should be able to reach by forgetting something.

The server binds inside the pod and is reached through a ClusterIP Service by
the API only — it is never exposed at the ingress.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from devai.sandbox.workspace import WORKSPACE_PORT, WORKSPACE_ROOT, WorkspaceError, WorkspaceFiles, run_shell

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-DevAI-Workspace-Token"


class _Path(BaseModel):
    path: str = "."


class _Write(BaseModel):
    path: str
    content: str


class _Replace(BaseModel):
    path: str
    old: str
    new: str


class _Search(BaseModel):
    needle: str
    path: str = "."


class _Command(BaseModel):
    command: str
    timeout: float = 120.0


def create_workspace_app(*, root: Path | str = WORKSPACE_ROOT, token: str) -> FastAPI:
    if not token:
        raise ValueError("a workspace needs a capability token; refusing to serve without one")

    files = WorkspaceFiles(root=root)
    app = FastAPI(title="DevAI sandbox workspace", docs_url=None, redoc_url=None)

    def authorize(supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="workspace token missing or invalid")

    def guard(fn: Any) -> Any:
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/file/read")
    async def file_read(body: _Path, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, str]:
        authorize(x_devai_workspace_token)
        return {"path": body.path, "content": guard(lambda: files.read(body.path))}

    @app.post("/file/write")
    async def file_write(body: _Write, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        guard(lambda: files.write(body.path, body.content))
        return {"path": body.path, "bytes": len(body.content)}

    @app.post("/file/list")
    async def file_list(body: _Path, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return {"path": body.path, "entries": guard(lambda: files.list(body.path))}

    @app.post("/file/search")
    async def file_search(body: _Search, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return {"hits": guard(lambda: files.search(body.needle, body.path))}

    @app.post("/file/replace")
    async def file_replace(
        body: _Replace, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return {"path": body.path, "replaced": guard(lambda: files.replace(body.path, body.old, body.new))}

    @app.post("/file/delete")
    async def file_delete(body: _Path, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        guard(lambda: files.delete(body.path))
        return {"path": body.path, "deleted": True}

    @app.post("/shell/exec")
    async def shell_exec(body: _Command, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        # A non-zero exit is a result, not a server error — the caller is a model.
        return run_shell(body.command, root=files.root, timeout=body.timeout)

    return app


def main() -> None:
    import uvicorn

    app = create_workspace_app(
        root=os.getenv("DEVAI_WORKSPACE_ROOT", WORKSPACE_ROOT),
        token=os.getenv("DEVAI_WORKSPACE_TOKEN", ""),
    )
    uvicorn.run(app, host="0.0.0.0", port=WORKSPACE_PORT)  # noqa: S104 — pod-local; reached via ClusterIP only


if __name__ == "__main__":
    main()


__all__ = ["TOKEN_HEADER", "create_workspace_app"]
