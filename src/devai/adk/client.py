"""Authenticated Python client for the sandbox lifecycle and eval surface."""

from __future__ import annotations

from typing import Any

import httpx

from devai.services.redact import redact_secrets


class AdkError(Exception):
    """Base error for the public ADK surface."""


class AdkAPIError(AdkError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = redact_secrets(detail)
        super().__init__(f"DevAI API returned {status_code}: {self.detail}")


class SandboxClient:
    """Small synchronous client used by scripts and the Typer CLI.

    Ownership is never accepted here: the server derives it from the authenticated
    session. A browser-session cookie is optional for remote use; local development
    can run with authentication disabled.
    """

    def __init__(
        self,
        *,
        base_url: str,
        session_cookie: str = "",
        token: str = "",
        timeout_seconds: float = 900.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        cookies = {"devai_session": session_cookie} if session_cookie else None
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            cookies=cookies,
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request_object("POST", "/api/sandboxes", json=spec)

    def get(self, sandbox_id: str) -> dict[str, Any]:
        return self._request_object("GET", f"/api/sandboxes/{sandbox_id}")

    def invoke(self, sandbox_id: str, message: str) -> dict[str, Any]:
        return self._request_object("POST", f"/api/sandboxes/{sandbox_id}/invoke", json={"message": message})

    def traces(self, sandbox_id: str) -> list[dict[str, Any]]:
        body = self._request("GET", f"/api/sandboxes/{sandbox_id}/traces")
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            raise AdkAPIError(502, "trace response was not a list")
        return [{str(key): value for key, value in item.items()} for item in body]

    def test(self, sandbox_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request_object("POST", f"/api/sandboxes/{sandbox_id}/evals", json={"cases": cases})

    def evaluate(self, sandbox_id: str, suite_name: str, suite_version: str) -> dict[str, Any]:
        return self._request_object(
            "POST",
            "/api/evaluations",
            json={
                "suite": {"name": suite_name, "version": suite_version},
                "sandbox_id": sandbox_id,
            },
        )

    def publish_agent(
        self,
        manifest: dict[str, Any],
        *,
        overwrite: bool = False,
        override_reason: str = "",
    ) -> dict[str, Any]:
        headers = None
        if override_reason:
            headers = {
                "x-devai-eval-gate-override": "true",
                "x-devai-eval-gate-override-reason": override_reason,
            }
        path = "/api/registry/agents?overwrite=true" if overwrite else "/api/registry/agents"
        return self._request_object("POST", path, json=manifest, headers=headers)

    def destroy(self, sandbox_id: str) -> dict[str, Any]:
        return self._request_object("DELETE", f"/api/sandboxes/{sandbox_id}")

    def _request_object(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = self._request(method, path, json=json, headers=headers)
        if not isinstance(body, dict):
            raise AdkAPIError(502, "DevAI API response was not an object")
        return {str(key): value for key, value in body.items()}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        try:
            response = self._http.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as error:
            raise AdkAPIError(0, str(error)) from error
        if response.status_code >= 400:
            try:
                payload = response.json()
                detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            except ValueError:
                detail = response.text
            raise AdkAPIError(response.status_code, str(detail))
        try:
            body: object = response.json()
            return body
        except ValueError as error:
            raise AdkAPIError(502, "DevAI API returned invalid JSON") from error


__all__ = ["AdkAPIError", "AdkError", "SandboxClient"]
