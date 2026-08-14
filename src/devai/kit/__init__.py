"""DevAI's integration surface for `tesserix-adk` — the agent runtime library.

Distinct from `devai.adk`, which authors registry artifacts. This package is
where the kit is consumed: version selection, provider binding, trace mapping.
The kit owns what happens inside one agent run; DevAI owns everything between
runs, so nothing here reimplements a kit primitive.
"""

from devai.kit.versions import ADK_REPO, AdkVersionCatalogue, UnknownAdkVersion, normalize

__all__ = ["ADK_REPO", "AdkVersionCatalogue", "UnknownAdkVersion", "normalize"]
