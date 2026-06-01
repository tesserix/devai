"""Identity adapter ABC + canonical auth result.

The identity family abstracts *how a human proves who they are* so the rest
of DevAI doesn't care. In production that's GIP/Keycloak (handled by the
auth-bff and the existing dashboard OAuth flow). On a local kind sandbox —
where there's no GIP tenant, no Keycloak realm, no auth-bff — we swap in a
``local_db`` backend: a small fixed set of username/password team logins.

Only ``local_db`` actually authenticates here; prod selects ``noop`` because
its auth is terminated upstream (auth-bff) and never reaches this adapter.
That keeps the switch a pure config flag: ``DEVAI_AUTH_PROVIDER``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class AuthResult:
    """A successfully authenticated principal, shaped to match the Redis
    session the dashboard writes (see ``dashboard/routes.py``) so the
    existing ``extract_principal`` reads it with no changes."""

    login: str
    email: str
    name: str = ""
    roles: list[str] = field(default_factory=list)

    def to_session(self, provider: str) -> dict:
        return {
            "auth_provider": provider,
            "user_login": self.login,
            "user_name": self.name or self.login,
            "user_email": self.email,
            "avatar_url": "",
            "roles": list(self.roles),
        }


class IdentityAdapter(ABC):
    """Minimum surface every identity backend implements."""

    #: provider tag stamped into the session / Principal.auth_provider
    provider: str = "unknown"

    @abstractmethod
    def login_config(self) -> dict:
        """Public (non-secret) config the login page reads to decide which
        UI to render. Must include a ``mode`` key (e.g. ``local_db`` / ``gip``)."""

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> AuthResult | None:
        """Return an ``AuthResult`` on valid credentials, else ``None``.
        Never raises on bad credentials — a wrong password is ``None``."""


__all__ = ["AuthResult", "IdentityAdapter"]
