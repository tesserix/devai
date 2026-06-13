"""Entrypoint: ``python -m devai.mcpbridge`` runs the bridge with uvicorn."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from devai.config import settings
    from devai.mcpbridge.app import create_bridge_app

    app = create_bridge_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=int(getattr(settings, "mcpbridge_port", 8099)))  # noqa: S104


if __name__ == "__main__":
    main()
