"""The forward proxy served inside a sandbox's proxy pod.

Small on purpose: it answers CONNECT and absolute-URI HTTP, checks the host
against the allowlist, and records every attempt. A refusal is an immediate 403
naming the host — an agent that gets a legible refusal can report the missing
domain, where one that gets a hang reports nothing useful.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from devai.sandbox.egress import DEFAULT_ALLOWLIST, PROXY_PORT, host_allowed

logger = logging.getLogger(__name__)

_HEADER_LIMIT = 64 * 1024
_CHUNK = 64 * 1024

Recorder = Callable[[dict[str, Any]], None]


def _log_line(record: dict[str, Any]) -> None:
    # One JSON line per attempt, so the run trace can join on sandbox id.
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _target(request_line: str) -> tuple[str, str, int] | None:
    """(method, host, port) for a request this proxy can act on."""
    parts = request_line.split()
    if len(parts) < 3:
        return None
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        host, _, port = target.partition(":")
        return ("CONNECT", host, int(port) if port.isdigit() else 443)
    if "://" not in target:
        return None
    rest = target.split("://", 1)[1]
    authority = rest.split("/", 1)[0]
    host, _, port = authority.partition(":")
    return (method, host, int(port) if port.isdigit() else 80)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: str, body: str) -> None:
    payload = body.encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    writer.close()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allowlist: list[str],
    on_record: Recorder,
) -> None:
    try:
        request_line = (await asyncio.wait_for(reader.readline(), timeout=30)).decode("latin-1").strip()
    except (TimeoutError, ConnectionError):
        writer.close()
        return

    parsed = _target(request_line)
    if parsed is None:
        on_record({"host": "", "port": 0, "method": "", "allowed": False, "reason": "unparseable"})
        await _refuse(writer, "400 Bad Request", "devai sandbox egress: unparseable request\n")
        return

    method, host, port = parsed
    allowed = host_allowed(host, allowlist)
    on_record({"host": host, "port": port, "method": method, "allowed": allowed})
    if not allowed:
        await _refuse(
            writer,
            "403 Forbidden",
            f"devai sandbox egress: {host} is not on this sandbox's allowlist.\n"
            "Add it to the sandbox spec's allow_domains if the run genuinely needs it.\n",
        )
        return

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=30)
    except (OSError, TimeoutError) as e:
        await _refuse(writer, "502 Bad Gateway", f"devai sandbox egress: cannot reach {host}:{port} ({e})\n")
        return

    if method == "CONNECT":
        # Drain the CONNECT headers before handing the socket to the tunnel.
        while line := await reader.readline():
            if line in (b"\r\n", b"\n"):
                break
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    else:
        upstream_writer.write(request_line.encode("latin-1") + b"\r\n")
        headers = await reader.readuntil(b"\r\n\r\n") if not reader.at_eof() else b"\r\n"
        upstream_writer.write(headers)
        await upstream_writer.drain()

    await asyncio.gather(
        _pipe(reader, upstream_writer),
        _pipe(upstream_reader, writer),
        return_exceptions=True,
    )


async def serve_proxy(
    allowlist: list[str] | tuple[str, ...],
    *,
    host: str = "0.0.0.0",  # noqa: S104 — pod-local; reached via ClusterIP only
    port: int = PROXY_PORT,
    on_record: Recorder = _log_line,
) -> asyncio.AbstractServer:
    entries = list(allowlist)

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _handle(reader, writer, entries, on_record)
        except Exception:  # noqa: BLE001 — one bad client must not take the proxy down
            logger.warning("egress proxy: connection failed", exc_info=True)
            writer.close()

    return await asyncio.start_server(client, host, port, limit=_HEADER_LIMIT)


def _load_allowlist() -> list[str]:
    path = os.getenv("DEVAI_EGRESS_ALLOWLIST_FILE", "")
    if path and os.path.exists(path):
        with open(path) as fh:
            return [line.strip() for line in fh if line.strip()]
    return list(DEFAULT_ALLOWLIST)


async def _main() -> None:
    server = await serve_proxy(_load_allowlist())
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()


__all__ = ["serve_proxy"]
