"""The models a user may pick, and the promise that picking one works.

The catalog is only worth having if it cannot drift from the routing layer:
every model it offers under a provider must be one that provider can actually
serve, and every tier model the router would choose must be offerable.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.adapters.llm.capabilities import PROVIDER_TIER_MODELS
from devai.adapters.llm.model_policy import provider_serves
from devai.catalog.models import CATALOG, default_provider, models_for, provider_ids
from devai.catalog.routes import router
from devai.config import Settings


def test_every_offered_model_is_one_its_provider_can_serve() -> None:
    for provider in provider_ids():
        for model in models_for(provider):
            assert provider_serves(provider, model.id), f"{provider} cannot serve {model.id}"


def test_every_model_the_router_would_choose_is_offerable() -> None:
    for provider, tiers in PROVIDER_TIER_MODELS.items():
        offered = {m.id for m in models_for(provider)}
        for tier, model in tiers.items():
            assert model in offered, f"{provider}/{tier} routes to {model}, which is not offered"


def test_the_same_model_is_offered_under_every_provider_that_serves_it() -> None:
    # Claude on Anthropic direct and Claude through the gateway are the same
    # model on two credentials — the point of the picker being provider-aware.
    sonnet = "claude-sonnet-4-6"
    assert sonnet in {m.id for m in models_for("anthropic")}
    assert sonnet in {m.id for m in models_for("gateway")}


def test_each_provider_has_a_default_its_own_list_contains() -> None:
    for provider in provider_ids():
        entry = CATALOG[provider]
        assert entry.default_model in {m.id for m in entry.models}


def test_models_for_an_unknown_provider_is_empty_not_an_error() -> None:
    assert models_for("does-not-exist") == ()


def _client() -> TestClient:
    app = FastAPI()
    app.state.config = Settings()
    app.include_router(router)
    return TestClient(app)


def test_the_route_lists_providers_with_their_models() -> None:
    body = _client().get("/api/models").json()

    assert body["default_provider"] == default_provider()
    anthropic = next(p for p in body["providers"] if p["id"] == "anthropic")
    assert anthropic["label"] == "Anthropic"
    assert {m["id"] for m in anthropic["models"]} == {m.id for m in models_for("anthropic")}
    assert all("tier" in m and "label" in m for m in anthropic["models"])


def test_the_route_says_which_providers_have_credentials() -> None:
    # Nothing is configured in a bare Settings, so nothing may claim to be
    # connected — a picker that offers a provider the tenant cannot use is worse
    # than one that greys it out.
    body = _client().get("/api/models").json()

    assert [p["id"] for p in body["providers"] if p["connected"]] == []
