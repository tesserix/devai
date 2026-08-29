"""Platform-owner surface — /api/admin/*.

Every route here is gated by :func:`devai.admin.routes.require_admin`, which
is applied to the router itself rather than per handler, so a route added
later cannot ship unguarded.
"""

from devai.admin.routes import router

__all__ = ["router"]
