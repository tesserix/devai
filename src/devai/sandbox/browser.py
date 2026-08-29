"""One Playwright browser shared by agents and the workspace desktop."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from devai.sandbox.workspace import WORKSPACE_ROOT

_ACTION_TIMEOUT_MS = 30_000
_MAX_SCREENSHOT_BYTES = 5_000_000
_MAX_CONTENT_BYTES = 2_000_000


class BrowserError(Exception):
    """A browser operation was unsafe, unavailable, or exceeded its bound."""


class BrowserDownloads:
    """Persist Playwright downloads on the shared workspace volume."""

    def __init__(self, root: Path | str = WORKSPACE_ROOT) -> None:
        self._root = Path(root).resolve()

    async def save(self, download: Any) -> str:
        self._root.mkdir(parents=True, exist_ok=True)
        name = Path(str(download.suggested_filename)).name or "download"
        target = self._root / name
        index = 2
        while target.exists():
            target = self._root / f"{Path(name).stem}-{index}{Path(name).suffix}"
            index += 1
        await download.save_as(str(target))
        return str(target.relative_to(self._root))


def _resolve(host: str) -> Iterable[str]:
    try:
        return {str(address[4][0]) for address in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise BrowserError(f"browser host could not be resolved: {host}") from error


def validate_browser_url(url: str, *, resolve: Callable[[str], Iterable[str]] = _resolve) -> str:
    """Reject non-web and internal destinations before Chromium sees them."""
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise BrowserError("browser URL is invalid") from error
    if parsed.scheme not in {"http", "https"}:
        raise BrowserError("browser navigation accepts only http and https URLs")
    if not parsed.hostname:
        raise BrowserError("browser URL needs a host")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserError("browser URLs cannot contain credentials")

    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return url

    addresses = tuple(resolve(host))
    if not addresses:
        raise BrowserError(f"browser host could not be resolved: {host}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise BrowserError(f"browser host returned an invalid address: {host}") from error
        if not ip.is_global:
            raise BrowserError("browser navigation to private or internal addresses is blocked")
    return url


class BrowserSession:
    """Serialised control of the headed Chromium rendered on the VNC display."""

    def __init__(self, *, root: Path | str = WORKSPACE_ROOT) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._downloads = BrowserDownloads(self._root)
        self._download_tasks: set[asyncio.Task[None]] = set()
        self._download_paths: list[str] = []

    def _capture_download(self, download: Any) -> None:
        async def save() -> None:
            self._download_paths.append(await self._downloads.save(download))

        self._download_tasks.add(asyncio.create_task(save()))

    async def _finish_downloads(self, since: int) -> list[str]:
        await asyncio.sleep(0)
        tasks = tuple(self._download_tasks)
        if tasks:
            await asyncio.gather(*tasks)
            self._download_tasks.difference_update(tasks)
        return self._download_paths[since:]

    async def _start(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        self._root.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        launch: dict[str, Any] = {
            "headless": False,
            "downloads_path": str(self._root),
            "args": ["--disable-dev-shm-usage"],
        }
        proxy = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
        if proxy:
            launch["proxy"] = {
                "server": proxy,
                "bypass": os.getenv("NO_PROXY", "localhost,127.0.0.1,::1"),
            }
        self._browser = await self._playwright.chromium.launch(**launch)
        self._context = await self._browser.new_context(accept_downloads=True)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(_ACTION_TIMEOUT_MS)
        self._page.on("download", self._capture_download)
        return self._page

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def navigate(self, url: str) -> dict[str, Any]:
        target = validate_browser_url(url)
        async with self._lock:
            page = await self._start()
            downloads = len(self._download_paths)
            try:
                response = await page.goto(target, wait_until="domcontentloaded", timeout=_ACTION_TIMEOUT_MS)
                return {
                    "url": page.url,
                    "title": await page.title(),
                    "status": response.status if response is not None else None,
                    "downloads": await self._finish_downloads(downloads),
                }
            except Exception as error:  # noqa: BLE001 — Playwright errors are a bounded tool result
                raise BrowserError("browser navigation failed") from error

    async def screenshot(self, *, full_page: bool = False) -> dict[str, Any]:
        async with self._lock:
            page = await self._start()
            image = await page.screenshot(type="png", full_page=full_page)
            if len(image) > _MAX_SCREENSHOT_BYTES:
                raise BrowserError("browser screenshot exceeded 5 MB")
            return {"media_type": "image/png", "base64": base64.b64encode(image).decode(), "url": page.url}

    async def click(self, selector: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._start()
            downloads = len(self._download_paths)
            await page.locator(selector).click(timeout=_ACTION_TIMEOUT_MS)
            return {
                "clicked": selector,
                "url": page.url,
                "downloads": await self._finish_downloads(downloads),
            }

    async def type(self, selector: str, text: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._start()
            await page.locator(selector).fill(text, timeout=_ACTION_TIMEOUT_MS)
            return {"typed": selector, "characters": len(text), "url": page.url}

    async def scroll(self, *, delta_x: float = 0, delta_y: float = 600) -> dict[str, Any]:
        async with self._lock:
            page = await self._start()
            await page.mouse.wheel(delta_x, delta_y)
            return {"delta_x": delta_x, "delta_y": delta_y, "url": page.url}

    async def get_content(self, *, selector: str = "") -> dict[str, Any]:
        async with self._lock:
            page = await self._start()
            content = (
                await page.locator(selector).inner_text(timeout=_ACTION_TIMEOUT_MS)
                if selector
                else await page.content()
            )
            if len(content.encode()) > _MAX_CONTENT_BYTES:
                raise BrowserError("browser content exceeded 2 MB")
            return {"content": content, "url": page.url}

    async def close(self) -> None:
        async with self._lock:
            await self._finish_downloads(len(self._download_paths))
            if self._context is not None:
                await self._context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._page = self._context = self._browser = self._playwright = None


__all__ = ["BrowserDownloads", "BrowserError", "BrowserSession", "validate_browser_url"]
