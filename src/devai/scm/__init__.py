"""Source Code Management abstraction layer.

Supports GitHub, GitLab, and Azure DevOps with pluggable auth.
"""

from devai.scm.base import SCMClient, SCMProvider, AuthMethod
from devai.scm.factory import create_scm_client

__all__ = ["SCMClient", "SCMProvider", "AuthMethod", "create_scm_client"]
