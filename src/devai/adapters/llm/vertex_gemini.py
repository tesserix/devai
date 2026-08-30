"""Vertex AI Gemini backend — REST + Application Default Credentials.

No vendor SDK: the wire format is plain JSON over HTTPS (httpx), and auth
is an OAuth bearer minted from ADC via ``google-auth`` (lazy-imported).
On GKE the pod's Workload Identity GSA supplies the credentials — no API
keys — and the in-VPC private DNS zone pins ``aiplatform.googleapis.com``
to the PSC endpoint, so inference traffic never leaves the VPC.

Model IDs are Vertex publisher models (``gemini-2.5-flash``, …). The
``x-goog-user-project`` header is always sent: GKE metadata tokens don't
need it, but impersonated/user ADC (local dev) does, and it's harmless
when redundant.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.llm.base import (
    LLMAdapter,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMUsage,
    ToolCall,
)
from devai.adapters.llm.gateway_routing import gateway_headers

logger = logging.getLogger(__name__)

_TIMEOUT_S = 120.0


class VertexGeminiLLMAdapter(LLMAdapter):
    provider_name = "vertex_gemini"

    def __init__(
        self,
        *,
        project: str,
        location: str = "global",
        default_model: str = "gemini-2.5-flash",
        embedding_model: str = "text-embedding-005",
        base_url: str = "",
        api_key: str = "",
        gateway_routed: bool = False,
    ) -> None:
        if not project:
            raise AdapterNotConfigured("vertex_gemini adapter requires DEVAI_VERTEX_PROJECT")
        self._project = project
        self._location = location or "global"
        self.default_model = default_model or "gemini-2.5-flash"
        self._embedding_model = embedding_model or "text-embedding-005"
        self._api_key = api_key
        self._gateway = bool(base_url)
        self._gateway_routed = gateway_routed
        self._creds: Any = None
        if not api_key and not base_url:
            # Keyless direct mode is the only one that needs google-auth.
            try:
                import google.auth  # noqa: F401, PLC0415
            except ImportError as e:  # pragma: no cover
                raise AdapterNotInstalled("vertex_gemini adapter requires google-auth for ADC mode") from e
        if base_url:
            # ai-gateway route — the gateway attaches GCP credentials and
            # passes caller headers through, so this client must send none.
            origin = base_url.rstrip("/")
        else:
            host = (
                "aiplatform.googleapis.com"
                if self._location == "global"
                else f"{self._location}-aiplatform.googleapis.com"
            )
            origin = f"https://{host}"
        self._base = f"{origin}/v1/projects/{project}/locations/{self._location}/publishers/google/models"

    # ── auth ──────────────────────────────────────────────────────────

    def _headers(self, extra: dict[str, Any] | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._gateway_routed:
            headers.update(gateway_headers(extra, provider=self.provider_name))
        if self._gateway:
            # Gateway owns Vertex auth (GCP token attach); sending a caller
            # x-goog-api-key alongside it makes Google reject with 401.
            return headers
        if self._api_key:
            headers["x-goog-api-key"] = self._api_key
            return headers
        if self._creds is None:
            import google.auth  # noqa: PLC0415

            self._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not self._creds.valid:
            from google.auth.transport.requests import Request  # noqa: PLC0415

            self._creds.refresh(Request())
        headers["Authorization"] = f"Bearer {self._creds.token}"
        headers["x-goog-user-project"] = self._project
        return headers

    # ── request mapping ───────────────────────────────────────────────

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == LLMRole.SYSTEM:
                # Folded into systemInstruction below.
                continue
            if msg.role == LLMRole.TOOL:
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": msg.name or msg.tool_call_id or "tool",
                                    "response": {"content": msg.content},
                                }
                            }
                        ],
                    }
                )
                continue
            parts: list[dict[str, Any]] = []
            if msg.content:
                parts.append({"text": msg.content})
            if msg.role == LLMRole.ASSISTANT and msg.tool_calls:
                parts.extend({"functionCall": {"name": tc.name, "args": dict(tc.arguments)}} for tc in msg.tool_calls)
            for img in msg.images:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": img.get("media_type", "image/png"),
                            "data": img.get("data", ""),
                        }
                    }
                )
            if not parts:
                continue
            contents.append({"role": "model" if msg.role == LLMRole.ASSISTANT else "user", "parts": parts})

        system_texts = [request.system] if request.system else []
        system_texts += [m.content for m in request.messages if m.role == LLMRole.SYSTEM and m.content]

        body: dict[str, Any] = {"contents": contents}
        if system_texts:
            body["systemInstruction"] = {"parts": [{"text": t} for t in system_texts]}
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": dict(t.parameters) or {"type": "object"},
                        }
                        for t in request.tools
                    ]
                }
            ]
        gen: dict[str, Any] = {}
        if request.max_tokens:
            gen["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            gen["temperature"] = request.temperature
        if request.top_p is not None:
            gen["topP"] = request.top_p
        if request.stop_sequences:
            gen["stopSequences"] = list(request.stop_sequences)
        if (request.response_format or {}).get("type") in ("json_object", "json"):
            gen["responseMimeType"] = "application/json"
        if gen:
            body["generationConfig"] = gen
        return body

    @staticmethod
    def _parse(payload: dict[str, Any], *, model: str, latency_ms: float) -> LLMResponse:
        candidates = payload.get("candidates") or [{}]
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        tool_calls = [
            ToolCall(
                id=f"{p['functionCall'].get('name', 'fn')}-{i}",
                name=p["functionCall"].get("name", ""),
                arguments=dict(p["functionCall"].get("args") or {}),
            )
            for i, p in enumerate(parts)
            if "functionCall" in p
        ]
        usage_md = payload.get("usageMetadata") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_md.get("promptTokenCount", 0)),
            completion_tokens=int(usage_md.get("candidatesTokenCount", 0)),
            total_tokens=int(usage_md.get("totalTokenCount", 0)),
            cached_tokens=int(usage_md.get("cachedContentTokenCount", 0)),
        )
        finish_raw = str(candidates[0].get("finishReason", "")).upper()
        extra: dict[str, Any] = {}
        if tool_calls:
            finish = "tool_use"
        elif finish_raw == "MAX_TOKENS":
            finish = "length"
        elif finish_raw in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT"):
            finish = "content_filter"
        elif finish_raw in ("UNEXPECTED_TOOL_CALL", "MALFORMED_FUNCTION_CALL"):
            # Gemini emits an EMPTY candidate here (e.g. the prompt told it to
            # call a tool the request never declared); treating it as a clean
            # stop made runs look like a successful empty answer.
            finish = "error"
            extra = {"error": f"model attempted an invalid tool call ({finish_raw})", "finish_raw": finish_raw}
        else:
            finish = "stop"
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish,
            model=str(payload.get("modelVersion", "") or model),
            provider="vertex_gemini",
            request_id=str(payload.get("responseId", "")),
            latency_ms=latency_ms,
            extra=extra,
        )

    # ── LLMAdapter surface ────────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        import httpx  # noqa: PLC0415

        model = request.model or self.default_model
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                f"{self._base}/{model}:generateContent",
                headers=self._headers(request.extra),
                json=self._body(request),
            )
        latency_ms = (time.monotonic() - started) * 1000.0
        if resp.status_code != 200:
            detail = resp.text[:500]
            logger.warning("vertex_gemini %s → HTTP %s: %s", model, resp.status_code, detail)
            return LLMResponse(
                text="",
                finish_reason="error",
                model=model,
                provider=self.provider_name,
                latency_ms=latency_ms,
                extra={"status_code": resp.status_code, "error": detail},
            )
        return self._parse(resp.json(), model=model, latency_ms=latency_ms)

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        import httpx  # noqa: PLC0415

        embed_model = model or self._embedding_model
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                f"{self._base}/{embed_model}:predict",
                headers=self._headers(),
                json={"instances": [{"content": t} for t in texts]},
            )
        resp.raise_for_status()
        return [list(p.get("embeddings", {}).get("values", [])) for p in resp.json().get("predictions", [])]

    async def list_models(self) -> list[dict[str, str]]:
        """Text-generation models from the Vertex publisher catalog.

        Lists google + anthropic publishers (the two MaaS families this
        project uses). Catalog listing works with any valid credential;
        whether a given model SERVES still depends on per-model enablement.
        """
        import httpx  # noqa: PLC0415

        out: list[dict[str, str]] = []
        skip = ("tts", "image", "live", "exp", "embedding", "ocr")
        try:
            host = self._base.split("/v1/")[0]
            async with httpx.AsyncClient(timeout=30.0) as client:
                for publisher in ("google", "anthropic"):
                    resp = await client.get(
                        f"{host}/v1beta1/publishers/{publisher}/models",
                        params={"pageSize": 200, "listAllVersions": "false"},
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        continue
                    for m in resp.json().get("publisherModels", []):
                        mid = m.get("name", "").split("/")[-1]
                        stage = m.get("launchStage", "")
                        if not mid or any(s in mid for s in skip):
                            continue
                        if stage not in ("GA", "PUBLIC_PREVIEW"):
                            continue
                        out.append({"id": mid, "display_name": f"{mid} ({publisher}, {stage})"})
            return out
        except Exception:  # noqa: BLE001
            logger.warning("vertex_gemini adapter list_models failed", exc_info=True)
            return []

    async def health_check(self) -> dict[str, Any]:
        try:
            self._headers()
            mode = "api-key" if self._api_key else ("gateway" if self._gateway else "ADC")
            return {
                "ok": True,
                "provider": self.provider_name,
                "detail": f"auth={mode}; endpoint {self._base.split('/v1/')[0]}",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}


__all__ = ["VertexGeminiLLMAdapter"]
