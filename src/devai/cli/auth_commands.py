"""Browser-assisted authentication for the DevAI CLI."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import keyring
import typer
from keyring.errors import KeyringError, PasswordDeleteError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from rich.console import Console

DEFAULT_DEVAI_URL = "https://devai.tesserix.app"
_KEYRING_SERVICE = "devai-cli"
_KEYRING_ACCOUNT = "active-session"
_MAX_CALLBACK_BODY_BYTES = 20 * 1024
_STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LOGIN_SESSION_MAX_AGE = timedelta(minutes=70)

console = Console()
auth_app = typer.Typer(name="auth", help="Sign in to DevAI and manage the CLI session.", no_args_is_help=True)


class CLIAuthError(Exception):
    """A safe-to-display CLI authentication failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class SignInProof(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id_token: SecretStr = Field(repr=False)
    pool: Literal["alm", "sre", "agentic"]
    tenant_id: str = Field(min_length=1, max_length=256)


class StoredSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    cookie: SecretStr = Field(repr=False)
    email: str = Field(min_length=1, max_length=320)
    expires_at: datetime

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _normalize_base_url(value)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session expiry must include a timezone")
        return value.astimezone(UTC)


class _HandoffPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id_token: SecretStr = Field(repr=False)
    pool: Literal["alm", "sre", "agentic"]
    tenant_id: str = Field(min_length=1, max_length=256)
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class CallbackResponse:
    status: int
    headers: dict[str, str]
    proof: SignInProof | None = None


class LoopbackCallback:
    """Pure request boundary used by the bounded loopback HTTP listener."""

    def __init__(self, *, state: str, expected_origin: str) -> None:
        if not _STATE_PATTERN.fullmatch(state):
            raise ValueError("state must be a 256-bit base64url value")
        self._state = state
        self._expected_origin = _normalize_base_url(expected_origin)
        self.proof: SignInProof | None = None

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> CallbackResponse:
        if path != "/callback":
            return CallbackResponse(status=404, headers={"Cache-Control": "no-store"})

        lowered = {key.lower(): value for key, value in headers.items()}
        if lowered.get("origin") != self._expected_origin:
            return CallbackResponse(status=403, headers={"Cache-Control": "no-store"})

        cors = {
            "Access-Control-Allow-Origin": self._expected_origin,
            "Cache-Control": "no-store",
            "Vary": "Origin",
        }
        if method == "OPTIONS":
            if lowered.get("access-control-request-method") != "POST":
                return CallbackResponse(status=405, headers=cors)
            cors.update(
                {
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "60",
                }
            )
            if lowered.get("access-control-request-private-network") == "true":
                cors["Access-Control-Allow-Private-Network"] = "true"
            return CallbackResponse(status=204, headers=cors)

        if method != "POST":
            return CallbackResponse(status=405, headers=cors)
        if lowered.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            return CallbackResponse(status=415, headers=cors)
        if not body or len(body) > _MAX_CALLBACK_BODY_BYTES:
            return CallbackResponse(status=413, headers=cors)

        try:
            handoff = _HandoffPayload.model_validate_json(body)
        except ValidationError:
            return CallbackResponse(status=400, headers=cors)
        if not hmac.compare_digest(handoff.state, self._state):
            return CallbackResponse(status=403, headers=cors)

        proof = SignInProof(id_token=handoff.id_token, pool=handoff.pool, tenant_id=handoff.tenant_id)
        self.proof = proof
        return CallbackResponse(status=204, headers=cors, proof=proof)


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class KeyringSessionStore:
    """Stores the encrypted DevAI session only in the operating-system keychain."""

    def __init__(self, *, backend: KeyringBackend | None = None) -> None:
        self._backend = backend or cast("KeyringBackend", keyring)

    def load(self, *, now: datetime | None = None) -> StoredSession | None:
        try:
            payload = self._backend.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except KeyringError as error:
            raise CLIAuthError("DevAI could not access the operating-system keychain") from error
        if not payload:
            return None
        try:
            session = StoredSession.model_validate_json(payload)
        except ValidationError as error:
            self.clear()
            raise CLIAuthError("The stored DevAI session was invalid and has been removed") from error
        current = now or datetime.now(UTC)
        if session.expires_at <= current:
            self.clear()
            return None
        return session

    def save(self, session: StoredSession) -> None:
        payload = json.dumps(
            {
                "base_url": session.base_url,
                "cookie": session.cookie.get_secret_value(),
                "email": session.email,
                "expires_at": session.expires_at.isoformat(),
            },
            separators=(",", ":"),
        )
        try:
            self._backend.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, payload)
        except KeyringError as error:
            raise CLIAuthError("DevAI could not save the session in the operating-system keychain") from error

    def clear(self) -> None:
        try:
            self._backend.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except PasswordDeleteError:
            return
        except KeyringError as error:
            raise CLIAuthError("DevAI could not remove the session from the operating-system keychain") from error


class _CallbackServer(ThreadingHTTPServer):
    callback: LoopbackCallback


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_OPTIONS(self) -> None:
        self._respond(body=b"")

    def do_POST(self) -> None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = _MAX_CALLBACK_BODY_BYTES + 1
        if length < 0 or length > _MAX_CALLBACK_BODY_BYTES:
            response = CallbackResponse(status=413, headers={"Cache-Control": "no-store"})
        else:
            response = self.server.callback.handle(
                method=self.command,
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=self.rfile.read(length),
            )
        self._write_response(response)

    def _respond(self, *, body: bytes) -> None:
        response = self.server.callback.handle(
            method=self.command,
            path=self.path,
            headers={key: value for key, value in self.headers.items()},
            body=body,
        )
        self._write_response(response)

    def _write_response(self, response: CallbackResponse) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        del args


def exchange_proof(
    *,
    base_url: str,
    proof: SignInProof,
    transport: httpx.BaseTransport | None = None,
) -> StoredSession:
    """Exchange a GIP proof and verify the resulting one-hour DevAI session."""
    normalized_url = _normalize_base_url(base_url)
    try:
        with httpx.Client(
            base_url=normalized_url,
            timeout=15.0,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.post(
                "/auth/auto-login",
                json={
                    "id_token": proof.id_token.get_secret_value(),
                    "expected_tenant_id": proof.tenant_id,
                    "pool": proof.pool,
                    "client_type": "cli",
                },
            )
            if response.status_code >= 400:
                raise CLIAuthError(
                    f"DevAI authentication failed ({response.status_code})",
                    status_code=response.status_code,
                )
            cookie = response.cookies.get("devai_session")
            if not cookie:
                raise CLIAuthError("DevAI authentication returned no session")

            me = client.get("/auth/me")
            if me.status_code >= 400:
                raise CLIAuthError(
                    f"DevAI session verification failed ({me.status_code})",
                    status_code=me.status_code,
                )
            identity = me.json()
    except CLIAuthError:
        raise
    except (httpx.HTTPError, ValueError) as error:
        raise CLIAuthError("DevAI authentication could not reach a valid server response") from error

    if not isinstance(identity, dict):
        raise CLIAuthError("DevAI session verification returned an invalid response")
    try:
        expires_at = datetime.fromisoformat(str(identity["exp"]))
        email = str(identity["email"])
    except (KeyError, TypeError, ValueError) as error:
        raise CLIAuthError("DevAI session verification returned an invalid identity") from error
    if expires_at.tzinfo is None:
        raise CLIAuthError("DevAI session verification returned an invalid expiry")
    now = datetime.now(UTC)
    if expires_at <= now or expires_at > now + _LOGIN_SESSION_MAX_AGE:
        raise CLIAuthError("DevAI authentication returned an invalid CLI session lifetime")

    return StoredSession(
        base_url=normalized_url,
        cookie=SecretStr(cookie),
        email=email,
        expires_at=expires_at,
    )


def verify_session(
    session: StoredSession,
    *,
    transport: httpx.BaseTransport | None = None,
) -> StoredSession:
    try:
        with httpx.Client(
            base_url=session.base_url,
            cookies={"devai_session": session.cookie.get_secret_value()},
            timeout=15.0,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.get("/auth/me")
    except httpx.HTTPError as error:
        raise CLIAuthError("DevAI session verification could not reach the server") from error
    if response.status_code >= 400:
        raise CLIAuthError(
            f"DevAI session is not valid ({response.status_code})",
            status_code=response.status_code,
        )
    return session


def login_with_browser(
    *,
    base_url: str,
    timeout_seconds: float,
    store: KeyringSessionStore | None = None,
) -> StoredSession:
    normalized_url = _normalize_base_url(base_url)
    state = secrets.token_urlsafe(32)
    callback = LoopbackCallback(state=state, expected_origin=normalized_url)
    server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    server.callback = callback
    callback_url = f"http://127.0.0.1:{server.server_port}/callback"
    login_url = f"{normalized_url}/login?{urlencode({'cli_callback': callback_url, 'cli_state': state})}"

    console.print("Opening your browser for DevAI sign-in…")
    if not webbrowser.open(login_url):
        console.print(f"Open this URL in your browser:\n{login_url}")

    deadline = time.monotonic() + timeout_seconds
    try:
        while callback.proof is None and time.monotonic() < deadline:
            server.timeout = min(0.5, max(0.0, deadline - time.monotonic()))
            server.handle_request()
    finally:
        server.server_close()
    if callback.proof is None:
        raise CLIAuthError("DevAI sign-in timed out; run `devai auth login` to try again")

    session = exchange_proof(base_url=normalized_url, proof=callback.proof)
    (store or KeyringSessionStore()).save(session)
    return session


def load_stored_session() -> StoredSession | None:
    """Load the active CLI session, returning no credential if keychain access is unavailable."""
    try:
        return KeyringSessionStore().load()
    except CLIAuthError:
        return None


@auth_app.command("login")
def login(
    api_url: str = typer.Option(DEFAULT_DEVAI_URL, "--api-url", envvar="DEVAI_API_URL"),
    timeout: float = typer.Option(180.0, "--timeout", min=30.0, max=600.0),
) -> None:
    """Sign in with the browser and save a one-hour session in the OS keychain."""
    try:
        session = login_with_browser(base_url=api_url, timeout_seconds=timeout)
    except (CLIAuthError, ValueError) as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=1) from error
    console.print(f"[green]Signed in[/] as {session.email}; session expires at {session.expires_at.isoformat()}")


@auth_app.command("status")
def status() -> None:
    """Verify the active DevAI CLI session."""
    store = KeyringSessionStore()
    try:
        session = store.load()
        if session is None:
            console.print("[yellow]Not signed in.[/] Run `devai auth login`.")
            raise typer.Exit(code=1)
        verify_session(session)
    except CLIAuthError as error:
        if error.status_code in {401, 403}:
            store.clear()
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=1) from error
    console.print(f"[green]Signed in[/] as {session.email}; session expires at {session.expires_at.isoformat()}")


@auth_app.command("logout")
def logout() -> None:
    """Remove the active DevAI CLI session from the OS keychain."""
    try:
        KeyringSessionStore().clear()
    except CLIAuthError as error:
        console.print(f"[red]{error}[/]")
        raise typer.Exit(code=1) from error
    console.print("[green]Signed out of DevAI.[/]")


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("DevAI API URL must be an origin without credentials, path, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("DevAI API URL must use HTTPS unless it is a loopback development server")
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default_port = 80 if parsed.scheme == "http" else 443
    netloc = hostname if parsed.port in {None, default_port} else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


__all__ = [
    "CLIAuthError",
    "KeyringSessionStore",
    "LoopbackCallback",
    "SignInProof",
    "StoredSession",
    "auth_app",
    "exchange_proof",
    "load_stored_session",
]
