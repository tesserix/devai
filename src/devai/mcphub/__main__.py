"""``python -m devai.mcphub`` — run the MCP Hub ASGI app under uvicorn.

Mirrors ``python -m devai.sre.server``: the Hub shares the ``devai`` image
(entrypoint ``python -m devai``), so its Deployment overrides the command to
this module. ``--factory`` style: we pass the app factory string so uvicorn
builds the app (and runs its lifespan: discovery + mount + periodic refresh).
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from devai.config import settings

    uvicorn.run(
        "devai.mcphub.app:create_hub_app",
        factory=True,
        host=getattr(settings, "host", "0.0.0.0"),
        port=getattr(settings, "mcp_hub_port", 8095),
        log_level=getattr(settings, "log_level", "info"),
        timeout_keep_alive=getattr(settings, "http_keepalive_timeout", 75),
    )


if __name__ == "__main__":
    main()
