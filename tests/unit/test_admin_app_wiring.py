from __future__ import annotations

from unittest.mock import MagicMock

from devai.admin.activity import ActivityMiddleware
from devai.config import Settings


def test_admin_router_is_registered():
    from devai.webhook.app import create_app

    app = create_app(MagicMock(), MagicMock(), Settings())
    # The generated schema is the version-stable view of what is mounted;
    # `app.routes` holds different shapes across Starlette releases.
    paths = app.openapi()["paths"]
    assert "/api/admin/overview" in paths
    assert "/api/admin/openpanel" in paths


def test_activity_middleware_is_installed():
    from devai.webhook.app import create_app

    app = create_app(MagicMock(), MagicMock(), Settings())
    assert any(m.cls is ActivityMiddleware for m in app.user_middleware)
