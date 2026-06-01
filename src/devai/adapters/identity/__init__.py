"""Identity adapter family — pluggable human auth.

Reference shape: ABC (``base``) + factory + one file per backend + mandatory
noop. Selected by ``DEVAI_AUTH_PROVIDER``. Only ``local_db`` authenticates
(kind sandbox); prod uses ``noop`` because auth is terminated at the auth-bff.
"""

from devai.adapters.identity.base import AuthResult, IdentityAdapter
from devai.adapters.identity.factory import create_identity_adapter
from devai.adapters.identity.noop import NoopIdentityAdapter

__all__ = [
    "AuthResult",
    "IdentityAdapter",
    "NoopIdentityAdapter",
    "create_identity_adapter",
]
