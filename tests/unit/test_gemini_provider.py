from types import SimpleNamespace

from devai.providers import gemini_provider


async def test_direct_gemini_uses_google_genai_async_client(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(text="generated")

    class FakeClient:
        def __init__(self) -> None:
            self.aio = SimpleNamespace(models=FakeModels())

    def make_client(*, api_key: str) -> FakeClient:
        calls["api_key"] = api_key
        return FakeClient()

    monkeypatch.setattr(gemini_provider.genai, "Client", make_client)
    config = SimpleNamespace(
        gemini_api_key="user-gemini-key",
        gcp_secret_gemini_api_key="gemini-api-key",
        gemini_model="gemini-2.5-flash",
        llm_gateway_required=False,
        llm_provider="gemini",
        openai_api_key="",
        overlaid_attrs=(),
    )

    provider = gemini_provider.GeminiProvider(config)
    result = await provider.generate(
        prompt="Inspect the repository",
        system="Return JSON",
        temperature=0.2,
        max_tokens=512,
    )

    assert result == "generated"
    assert calls["api_key"] == "user-gemini-key"
    assert calls["model"] == "gemini-2.5-flash"
    assert calls["contents"] == "Return JSON\n\nInspect the repository"
    generation_config = calls["config"]
    assert generation_config.temperature == 0.2
    assert generation_config.max_output_tokens == 512
