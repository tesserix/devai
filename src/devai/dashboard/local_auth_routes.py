"""Local username/password auth endpoints — LOCAL/kind sandbox ONLY.

Mounted at ``/auth`` (by ``webhook/app.py``) only when
``DEVAI_AUTH_PROVIDER=local_db``. The dashboard proxies ``/auth/*`` here in
local mode (see dashboard next.config.ts). Issues the SAME ``devai_session``
Redis session the production OAuth flow writes, so ``extract_principal`` —
and therefore every downstream authorization/role/team check — works
unchanged. Prod never mounts this router; its ``/auth/*`` goes to the
auth-bff (GIP) as before.
"""

from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from devai.adapters.identity import create_identity_adapter

logger = logging.getLogger(__name__)

# No /dashboard prefix: the dashboard proxies /auth/* straight here.
router = APIRouter(prefix="/auth", tags=["local-auth"])

_SESSION_TTL = 86400  # 24h, matches the OAuth flow


def _adapter(request: Request):
    # Reads seeded users from the devai_local_users table when the DB pool is
    # available (the local deploy), else the in-memory env roster.
    pool = getattr(request.app.state, "db_pool", None)
    return create_identity_adapter(request.app.state.config, pool=pool)


class LoginBody(BaseModel):
    username: str
    password: str


@router.get("/config")
async def auth_config(request: Request) -> dict:
    """Tell the login page which UI to render (local_db → password form)."""
    return _adapter(request).login_config()


@router.post("/login")
async def auth_login(request: Request, body: LoginBody) -> dict:
    """Validate username/password, mint a session, set the cookie."""
    adapter = _adapter(request)
    result = await adapter.authenticate(body.username, body.password)
    if result is None:
        # Uniform error — don't leak which of user/pass was wrong.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    session_id = secrets.token_urlsafe(48)
    redis = request.app.state.state_manager.redis
    await redis.set(
        f"devai:session:{session_id}",
        json.dumps(result.to_session(adapter.provider)),
        ex=_SESSION_TTL,
    )

    from fastapi.responses import JSONResponse

    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "login": result.login,
                "name": result.name,
                "email": result.email,
                "roles": result.roles,
            },
        }
    )
    # secure=True: local sandbox is served over TLS (https://…:8443).
    response.set_cookie(
        "devai_session",
        session_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=_SESSION_TTL,
    )
    logger.info("local_db login: %s (roles=%s)", result.login, result.roles)
    return response


@router.get("/logout")
async def auth_logout(request: Request):
    """Clear the session + cookie, bounce back to the dashboard."""
    from fastapi.responses import RedirectResponse

    session_id = request.cookies.get("devai_session")
    if session_id:
        await request.app.state.state_manager.redis.delete(f"devai:session:{session_id}")
    response = RedirectResponse("/")
    response.delete_cookie("devai_session")
    return response


__all__ = ["router"]
