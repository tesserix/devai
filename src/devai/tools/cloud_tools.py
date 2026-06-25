"""Cloud account tools — query the CALLER's connected cloud accounts.

Resolve the user's Cloud Account connectors (Settings → Cloud Account), build
the right ``adapters.cloud`` backend per call (per-user creds, never cached),
and answer identity + scope (projects/accounts/subscriptions) questions.

Registered into ``devai.tools.registry`` so a spec can opt in via
``allowed_tools: [cloud_list_accounts, cloud_identity, cloud_list_scopes]``.
Every call carries the caller's email (ToolContext.triggered_by) and is
strictly scoped to that user's connectors.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.tools.registry import Handler, ToolContext, register

logger = logging.getLogger(__name__)


def _dump(v: Any) -> str:
    try:
        return json.dumps(v, indent=2, default=str)
    except Exception:  # noqa: BLE001
        return str(v)


def _email(ctx: ToolContext) -> str:
    return (ctx.triggered_by or "").strip()


def _list_accounts_factory(ctx: ToolContext) -> Handler:
    async def handler(_: dict[str, Any]) -> str:
        email = _email(ctx)
        if "@" not in email:
            return "ERROR: this call carries no user identity"
        from devai.settings.connections import user_connections

        conns = await user_connections(email, "cloud")
        return _dump(
            [{"name": c.get("cloud_name") or c.get("instance_id"), "provider": c.get("provider", "")} for c in conns]
        )

    return handler


def _identity_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        adapter, problem = await _resolve(ctx, str(args.get("account") or ""))
        if problem:
            return problem
        return _dump(await adapter.identity())

    return handler


def _scopes_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        adapter, problem = await _resolve(ctx, str(args.get("account") or ""))
        if problem:
            return problem
        return _dump(await adapter.list_scopes())

    return handler


async def _resolve(ctx: ToolContext, name: str) -> tuple[Any, str]:
    email = _email(ctx)
    if "@" not in email:
        return None, "ERROR: this call carries no user identity"
    from devai.adapters.cloud import create_cloud_adapter
    from devai.settings.connections import user_cloud, user_cloud_names

    conn = await user_cloud(email, name)
    if conn is None:
        names = await user_cloud_names(email)
        return None, (
            f"ERROR: no connected cloud account named {name!r} for {email}. "
            + (f"Available: {', '.join(names)}" if names else "Connect one in Settings → Cloud Account.")
        )
    return create_cloud_adapter(conn), ""


_ACCOUNT = {
    "type": "string",
    "description": "Optional — the name of one of YOUR connected cloud accounts. Omit if you have exactly one.",
}


def register_cloud_tools(*, overwrite: bool = False) -> None:
    register(
        "cloud_list_accounts",
        "List the cloud accounts (GCP/AWS/Azure) YOU connected in Settings → Cloud Account.",
        {"type": "object", "properties": {}},
        _list_accounts_factory,
        overwrite=overwrite,
    )
    register(
        "cloud_identity",
        "Resolve who a connected cloud account authenticates as (SA/principal + project/account/subscription).",
        {"type": "object", "properties": {"account": _ACCOUNT}},
        _identity_factory,
        overwrite=overwrite,
    )
    register(
        "cloud_list_scopes",
        "List a connected cloud account's billing/ownership scopes — GCP projects, AWS account, Azure subscriptions.",
        {"type": "object", "properties": {"account": _ACCOUNT}},
        _scopes_factory,
        overwrite=overwrite,
    )


register_cloud_tools()

__all__ = ["register_cloud_tools"]
