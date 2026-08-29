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
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from devai.sandbox.browser import BrowserError, BrowserSession
from devai.sandbox.browser_desktop import BrowserDesktop
from devai.sandbox.browser_proxy import TOKEN_HEADER, proxy_desktop_request, proxy_desktop_socket
from devai.sandbox.preview import detect_ports
from devai.sandbox.workspace import WORKSPACE_PORT, WORKSPACE_ROOT, WorkspaceError, WorkspaceFiles, run_shell

logger = logging.getLogger(__name__)


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


class _Fetch(BaseModel):
    port: int
    path: str = ""


class _Command(BaseModel):
    command: str
    timeout: float = 120.0


class _BrowserNavigate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)


class _BrowserScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_page: bool = False


class _BrowserSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(min_length=1, max_length=4096)


class _BrowserType(_BrowserSelector):
    text: str = Field(max_length=1_000_000)


class _BrowserScroll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta_x: float = Field(default=0, ge=-100_000, le=100_000)
    delta_y: float = Field(default=600, ge=-100_000, le=100_000)


class _BrowserContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(default="", max_length=4096)


class _BrowserController(Protocol):
    async def navigate(self, url: str) -> dict[str, Any]: ...

    async def screenshot(self, *, full_page: bool = False) -> dict[str, Any]: ...

    async def click(self, selector: str) -> dict[str, Any]: ...

    async def type(self, selector: str, text: str) -> dict[str, Any]: ...

    async def scroll(self, *, delta_x: float = 0, delta_y: float = 600) -> dict[str, Any]: ...

    async def get_content(self, *, selector: str = "") -> dict[str, Any]: ...


def create_workspace_app(
    *, root: Path | str = WORKSPACE_ROOT, token: str, browser: _BrowserController | None = None
) -> FastAPI:
    if not token:
        raise ValueError("a workspace needs a capability token; refusing to serve without one")

    files = WorkspaceFiles(root=root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        start = getattr(browser, "start", None)
        if start is not None:
            await start()
        yield
        close = getattr(browser, "close", None)
        if close is not None:
            await close()

    app = FastAPI(title="DevAI sandbox workspace", docs_url=None, redoc_url=None, lifespan=lifespan)

    def authorize(supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="workspace token missing or invalid")

    def guard(fn: Any) -> Any:
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def require_browser() -> _BrowserController:
        if browser is None:
            raise HTTPException(status_code=409, detail="this workspace has no browser")
        return browser

    async def drive(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        try:
            return await awaitable
        except BrowserError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001 — dependency detail must not cross the workspace boundary
            logger.warning("browser operation failed")
            raise HTTPException(status_code=502, detail="browser operation failed") from error

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

    @app.get("/preview/ports")
    async def preview_ports(x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return {"ports": detect_ports()}

    @app.post("/preview/fetch")
    async def preview_fetch(body: _Fetch, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        import httpx

        url = f"http://127.0.0.1:{body.port}/{body.path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.get(url)
        except Exception as e:  # noqa: BLE001 — an app that isn't up yet is a preview state, not a 500
            raise HTTPException(status_code=502, detail=f"nothing answered on port {body.port}: {e}") from e
        return {
            "status": resp.status_code,
            "body": resp.text,
            "content_type": resp.headers.get("content-type", "text/plain"),
        }

    @app.post("/shell/exec")
    async def shell_exec(body: _Command, x_devai_workspace_token: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        # A non-zero exit is a result, not a server error — the caller is a model.
        return run_shell(body.command, root=files.root, timeout=body.timeout)

    @app.post("/browser/navigate")
    async def browser_navigate(
        body: _BrowserNavigate, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().navigate(body.url))

    @app.post("/browser/screenshot")
    async def browser_screenshot(
        body: _BrowserScreenshot, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().screenshot(full_page=body.full_page))

    @app.post("/browser/click")
    async def browser_click(
        body: _BrowserSelector, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().click(body.selector))

    @app.post("/browser/type")
    async def browser_type(
        body: _BrowserType, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().type(body.selector, body.text))

    @app.post("/browser/scroll")
    async def browser_scroll(
        body: _BrowserScroll, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().scroll(delta_x=body.delta_x, delta_y=body.delta_y))

    @app.post("/browser/content")
    async def browser_content(
        body: _BrowserContent, x_devai_workspace_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authorize(x_devai_workspace_token)
        return await drive(require_browser().get_content(selector=body.selector))

    @app.api_route("/browser/desktop/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    async def browser_desktop(
        request: Request,
        path: str = "",
        x_devai_workspace_token: str | None = Header(default=None),
    ) -> Response:
        authorize(x_devai_workspace_token)
        if browser is None:
            raise HTTPException(status_code=409, detail="this workspace has no browser")
        return await proxy_desktop_request(request, path)

    @app.websocket("/browser/desktop/{path:path}")
    async def browser_desktop_socket(websocket: WebSocket, path: str = "") -> None:
        try:
            authorize(websocket.headers.get(TOKEN_HEADER))
        except HTTPException:
            await websocket.close(code=1008)
            return
        if browser is None:
            await websocket.close(code=1008)
            return
        await proxy_desktop_socket(websocket, path)

    return app


def main() -> None:
    import uvicorn

    root = os.getenv("DEVAI_WORKSPACE_ROOT", WORKSPACE_ROOT)
    browser_enabled = os.getenv("DEVAI_WORKSPACE_BROWSER", "").lower() == "true"
    app = create_workspace_app(
        root=root,
        token=os.getenv("DEVAI_WORKSPACE_TOKEN", ""),
        browser=BrowserSession(root=root) if browser_enabled else None,
    )
    desktop = BrowserDesktop() if browser_enabled else nullcontext()
    with desktop:
        uvicorn.run(app, host="0.0.0.0", port=WORKSPACE_PORT)  # noqa: S104 — pod-local; ClusterIP only


if __name__ == "__main__":
    main()


__all__ = ["TOKEN_HEADER", "create_workspace_app"]
