"""SCM client factory — creates the right provider based on configuration.

Config-driven instantiation:
  DEVAI_SCM_PROVIDER=github|gitlab|azure_devops
  DEVAI_SCM_BASE_URL=https://api.github.com (or self-hosted URL)
  DEVAI_SCM_AUTH_METHOD=github_app|pat|oauth|ado_pat|gitlab_token
  DEVAI_SCM_TOKEN=ghp_xxx / glpat-xxx / ADO PAT

For GitHub App auth (automated, org-wide):
  DEVAI_GITHUB_APP_ID=12345
  DEVAI_GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----...
  DEVAI_GITHUB_APP_INSTALLATION_ID=67890

Connection flow:
  ┌──────────────┐
  │ User creates  │
  │ requirement   │──→ Webhook arrives
  │ issue on any  │      │
  │ SCM platform  │      ▼
  └──────────────┘  ┌─────────────────┐
                    │ create_scm_client│
                    │ (reads config)   │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ GitHub   │  │ GitLab   │  │ Azure    │
        │ Client   │  │ Client   │  │ DevOps   │
        │          │  │          │  │ Client   │
        │ App/PAT/ │  │ PAT/     │  │ PAT      │
        │ OAuth    │  │ OAuth    │  │          │
        └──────────┘  └──────────┘  └──────────┘
              │              │              │
              └──────────────┼──────────────┘
                             │
                      Same SCMClient interface
                      → All agents work identically
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from devai.scm.base import AuthMethod, SCMClient, SCMProvider

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)

# Default base URLs per provider
DEFAULT_URLS = {
    SCMProvider.GITHUB: "https://api.github.com",
    SCMProvider.GITLAB: "https://gitlab.com",
    SCMProvider.AZURE_DEVOPS: "https://dev.azure.com",
}


def create_scm_client(config: "Settings") -> SCMClient:
    """Create an SCM client from the application configuration.

    Reads config.scm_provider and config.scm_auth_method to determine
    which concrete client to instantiate. Falls back to GitHub + GitHub App
    for backward compatibility.

    Returns:
        A connected SCMClient instance ready for API calls.
    """
    provider = SCMProvider(getattr(config, "scm_provider", "github"))
    auth_method = AuthMethod(getattr(config, "scm_auth_method", "github_app"))
    base_url = getattr(config, "scm_base_url", "") or DEFAULT_URLS.get(provider, "")
    token = getattr(config, "scm_token", "")

    logger.info("Creating SCM client: provider=%s, auth=%s, url=%s", provider, auth_method, base_url)

    if provider == SCMProvider.GITHUB:
        from devai.scm.github_client import GitHubSCMClient

        return GitHubSCMClient(
            base_url=base_url,
            auth_method=auth_method,
            token=token,
            app_id=config.github_app_id,
            app_private_key=config.github_app_private_key,
            installation_id=config.github_app_installation_id,
        )

    if provider == SCMProvider.GITLAB:
        from devai.scm.gitlab_client import GitLabSCMClient

        return GitLabSCMClient(
            base_url=base_url,
            auth_method=auth_method,
            token=token,
        )

    if provider == SCMProvider.AZURE_DEVOPS:
        from devai.scm.ado_client import AzureDevOpsSCMClient

        org = getattr(config, "scm_organization", config.github_org)
        return AzureDevOpsSCMClient(
            base_url=base_url,
            token=token,
            organization=org,
        )

    raise ValueError(f"Unknown SCM provider: {provider}")


def create_scm_from_connection(
    provider: str,
    auth_method: str,
    base_url: str = "",
    token: str = "",
    app_id: int = 0,
    app_private_key: str = "",
    installation_id: int = 0,
    organization: str = "",
) -> SCMClient:
    """Create an SCM client from explicit connection parameters.

    Used by the dashboard when a user connects a new repo/org
    with their own credentials.
    """
    scm_provider = SCMProvider(provider)
    scm_auth = AuthMethod(auth_method)
    url = base_url or DEFAULT_URLS.get(scm_provider, "")

    if scm_provider == SCMProvider.GITHUB:
        from devai.scm.github_client import GitHubSCMClient

        return GitHubSCMClient(
            base_url=url, auth_method=scm_auth, token=token,
            app_id=app_id, app_private_key=app_private_key,
            installation_id=installation_id,
        )

    if scm_provider == SCMProvider.GITLAB:
        from devai.scm.gitlab_client import GitLabSCMClient

        return GitLabSCMClient(base_url=url, auth_method=scm_auth, token=token)

    if scm_provider == SCMProvider.AZURE_DEVOPS:
        from devai.scm.ado_client import AzureDevOpsSCMClient

        return AzureDevOpsSCMClient(base_url=url, token=token, organization=organization)

    raise ValueError(f"Unknown SCM provider: {provider}")
