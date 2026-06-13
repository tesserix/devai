"""GitOps adapter family — Argo CD / Kargo / Flux CD behind one ABC.

Public surface:

    from devai.adapters.gitops import (
        GitOpsAdapter, create_gitops_adapter, create_gitops_adapters,
    )

Backends are lazy-imported by the factory; importing this package pulls in
no kubectl call and no provider module.
"""

from devai.adapters.gitops.base import GitOpsAdapter
from devai.adapters.gitops.factory import KNOWN_PROVIDERS, create_gitops_adapter, create_gitops_adapters
from devai.adapters.gitops.noop import NoopGitOpsAdapter

__all__ = [
    "KNOWN_PROVIDERS",
    "GitOpsAdapter",
    "NoopGitOpsAdapter",
    "create_gitops_adapter",
    "create_gitops_adapters",
]
