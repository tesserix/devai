from __future__ import annotations

from unittest.mock import MagicMock

from devai.admin.activity import ActivityMiddleware
from devai.config import Settings


def _paths(routes) -> set[str]:
    """Collect route paths. Newer Starlette wraps included routers in a
    holder that carries nested routes instead of a path of its own."""
    found: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            found.add(path)
        found |= _paths(getattr(route, "routes", ()) or ())
    return found


def test_admin_router_is_registered():
    from devai.webhook.app import create_app

    app = create_app(MagicMock(), MagicMock(), Settings())
    paths = _paths(app.routes)
    assert "/api/admin/overview" in paths
    assert "/api/admin/openpanel" in paths


def test_activity_middleware_is_installed():
    from devai.webhook.app import create_app

    app = create_app(MagicMock(), MagicMock(), Settings())
    assert any(m.cls is ActivityMiddleware for m in app.user_middleware)
