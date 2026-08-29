"""Integration tests for PipelineService + the /api/pipeline/* route surface.

Uses an in-memory StateManager stub so the tests don't require Redis. The
real StateManager.persist_task is exercised by separate tests against a
running Redis (see tests/integration/).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.pipeline.service import PipelineService

REPO_ROOT = Path(__file__).resolve().parents[2]
BLUEPRINTS_DIR = REPO_ROOT / "blueprints"


class _StubSettings:
    """Minimal Settings object — only the attrs PipelineService reads."""

    pipeline_blueprint_dir = str(BLUEPRINTS_DIR)
    pipeline_default_blueprint = "pr-review"
    pipeline_pr_review_blueprint = "pr-review"
    pipeline_sre_blueprint = "sre-monitor"
    pipeline_concurrency = 2
    pipeline_default_stage_timeout = 30
    pipeline_event_ring_size = 100
    pipeline_task_ttl = 3600
    pipeline_label = "devai:automate"
    redis_url = "redis://localhost:6379"


class _MemoryStateManager:
    """Tiny in-memory stand-in for the Redis-backed StateManager."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.persist_calls = 0

    async def persist_task(self, task_dict: dict[str, Any], ttl: int | None = None) -> None:
        self.persist_calls += 1
        self.tasks[task_dict["id"]] = task_dict

    async def get_pipeline_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    async def list_pipeline_tasks(
        self, *, limit: int = 50, blueprint: str | None = None, repo: str | None = None
    ) -> list[dict[str, Any]]:
        out = list(self.tasks.values())
        if blueprint:
            out = [t for t in out if t.get("blueprint") == blueprint]
        if repo:
            out = [t for t in out if t.get("repo") == repo]
        out.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
        return out[:limit]


@pytest.fixture
def settings() -> _StubSettings:
    return _StubSettings()


@pytest.fixture
def state_manager() -> _MemoryStateManager:
    return _MemoryStateManager()


# ─────────────────────────────────────────────────────────────────────
# PipelineService lifecycle
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_start_and_stop_idempotent(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    await svc.start()  # second call is a no-op
    assert svc.list_blueprints(), "blueprints must be discoverable post-start"
    assert "alm-pipeline" in {b["name"] for b in svc.list_blueprints()}
    assert "sre-monitor" in {b["name"] for b in svc.list_blueprints()}
    await svc.stop()
    await svc.stop()  # second call is a no-op


@pytest.mark.asyncio
async def test_service_dispatch_persists_to_state_manager(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        task_id = await svc.dispatch(
            intent="check PR #42",
            blueprint="pr-review",
            repo="tesserix/devai",
            trigger_type="webhook",
        )
        # Wait briefly for the worker to pick it up and persist.
        for _ in range(40):
            if state_manager.persist_calls > 0 and task_id in state_manager.tasks:
                snap = state_manager.tasks[task_id]
                if snap.get("state") in {"completed", "stage_failed", "failed"}:
                    break
            await asyncio.sleep(0.05)
        assert state_manager.persist_calls > 0
        assert task_id in state_manager.tasks
        assert state_manager.tasks[task_id]["blueprint"] == "pr-review"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_dispatch_guard_dedupes_same_issue(settings):
    """A second dispatch for the same (repo, trigger_ref) within the window
    returns the FIRST run's id instead of starting a duplicate — the guard that
    stops the labeled+opened double-fire from racing two runs on one branch. A
    different issue is NOT deduped."""
    fakeredis = pytest.importorskip("fakeredis")
    sm = _MemoryStateManager()
    sm.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # type: ignore[attr-defined]
    svc = PipelineService(settings, state_manager=sm)
    await svc.start()
    try:
        first = await svc.dispatch(
            intent="build",
            blueprint="pr-review",
            repo="tesserix/devai",
            trigger_type="github_issue",
            agent_context={"trigger_ref": "42"},
        )
        dup = await svc.dispatch(
            intent="build",
            blueprint="pr-review",
            repo="tesserix/devai",
            trigger_type="github_issue",
            agent_context={"trigger_ref": "42"},
        )
        other = await svc.dispatch(
            intent="build",
            blueprint="pr-review",
            repo="tesserix/devai",
            trigger_type="github_issue",
            agent_context={"trigger_ref": "99"},
        )
        assert dup == first  # same issue → deduped to the first run
        assert other != first  # different issue → its own run
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_service_run_once_returns_completed_task(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        task = await svc.run_once(intent="sweep", blueprint="security-scan", repo="x/y")
        assert task.state.value in {"completed", "stage_failed"}
        assert task.id.startswith("devai-")
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_service_dispatch_rejects_unknown_blueprint(settings, state_manager):
    from devai.pipeline.pipeline import PipelineError

    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        with pytest.raises(PipelineError):
            await svc.dispatch(intent="x", blueprint="no-such-blueprint")
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_service_event_ring_records_completion(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        await svc.run_once(intent="scan", blueprint="security-scan", repo="x/y")
        events = svc.recent_events(limit=100)
        # The ring now carries typed envelopes (stage | agent_status | a2a);
        # phase is a stage-envelope field.
        phases = {e["phase"] for e in events if e.get("event_type", "stage") == "stage"}
        assert "started" in phases
        assert phases & {"completed", "skipped"}  # at least one stage finished
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_live_events_reach_every_api_replica(settings):
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    producer_state = _MemoryStateManager()
    producer_state.redis = fakeredis.aioredis.FakeRedis(  # type: ignore[attr-defined]
        server=server,
        decode_responses=True,
    )
    observer_state = _MemoryStateManager()
    observer_state.redis = fakeredis.aioredis.FakeRedis(  # type: ignore[attr-defined]
        server=server,
        decode_responses=True,
    )
    producer = PipelineService(settings, state_manager=producer_state)
    observer = PipelineService(settings, state_manager=observer_state)

    await producer.start()
    await observer.start()
    stream = observer.event_stream()
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    try:
        task = await producer.run_once(
            intent="scan",
            blueprint="security-scan",
            repo="x/y",
        )
        _, task_id, payload = await asyncio.wait_for(next_event, timeout=1.0)

        assert task_id == task.id
        assert payload is not None
        assert payload["event_type"] == "stage"
    finally:
        next_event.cancel()
        await stream.aclose()
        await producer.stop()
        await observer.stop()


# ─────────────────────────────────────────────────────────────────────
# FastAPI route surface — /api/pipeline/*
# ─────────────────────────────────────────────────────────────────────


def _build_app(svc: PipelineService | None) -> FastAPI:
    """Mount the pipeline router with a controllable pipeline_service."""
    from devai.pipeline.routes import router

    app = FastAPI()
    app.state.pipeline_service = svc
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_routes_503_when_service_disabled():
    app = _build_app(None)
    with TestClient(app) as client:
        resp = client.get("/api/pipeline/blueprints")
        assert resp.status_code == 503
        assert "pipeline runtime not enabled" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_routes_list_blueprints_and_stages(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        app = _build_app(svc)
        with TestClient(app) as client:
            bps = client.get("/api/pipeline/blueprints").json()
            names = {b["name"] for b in bps}
            assert {"alm-pipeline", "sre-monitor", "pr-review", "security-scan"}.issubset(names)

            stages = client.get("/api/pipeline/stages").json()
            assert len(stages) >= 30
            assert "noop" in stages
            assert "analyze_requirements" in stages
            assert "sre_correlate" in stages
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_routes_dispatch_and_lookup_a_task(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        app = _build_app(svc)
        with TestClient(app) as client:
            resp = client.post(
                "/api/pipeline/runs",
                json={
                    "intent": "review PR #42",
                    "blueprint": "pr-review",
                    "repo": "tesserix/devai",
                    "trigger_type": "webhook",
                },
            )
            assert resp.status_code == 202, resp.text
            task_id = resp.json()["task_id"]
            assert task_id.startswith("devai-")

            # Wait for completion
            for _ in range(40):
                detail = client.get(f"/api/pipeline/runs/{task_id}").json()
                if detail.get("state") in {"completed", "stage_failed"}:
                    break
                await asyncio.sleep(0.05)

            assert detail["blueprint"] == "pr-review"
            assert detail["state"] in {"completed", "stage_failed"}
            assert isinstance(detail["stage_events"], list)
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_routes_get_404_for_unknown_task(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        app = _build_app(svc)
        with TestClient(app) as client:
            resp = client.get("/api/pipeline/runs/devai-does-not-exist")
            assert resp.status_code == 404
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_routes_recent_events_endpoint(settings, state_manager):
    svc = PipelineService(settings, state_manager=state_manager)
    await svc.start()
    try:
        await svc.run_once(intent="sweep", blueprint="security-scan", repo="x/y")
        app = _build_app(svc)
        with TestClient(app) as client:
            events = client.get("/api/pipeline/events/recent?limit=20").json()
            assert events, "ring buffer should contain stage events post-run"
            sample = events[0]
            assert {"timestamp", "task_id", "stage", "phase"} <= set(sample.keys())
    finally:
        await svc.stop()


# ─────────────────────────────────────────────────────────────────────
# Trigger → blueprint mapping (the webhook helper)
# ─────────────────────────────────────────────────────────────────────


def test_select_blueprint_for_trigger_pr_maps_to_pr_review():
    from devai.webhook.routes import _select_blueprint_for_trigger

    class _Cfg:
        pipeline_default_blueprint = "alm-pipeline"
        pipeline_pr_review_blueprint = "pr-review"

    cfg = _Cfg()
    assert _select_blueprint_for_trigger(cfg, "pull_request") == "pr-review"
    assert _select_blueprint_for_trigger(cfg, "PR") == "pr-review"
    assert _select_blueprint_for_trigger(cfg, "github_issue") == "alm-pipeline"
    assert _select_blueprint_for_trigger(cfg, "manual") == "alm-pipeline"


# ── LLM preflight: fail fast when nothing is connected ────────────────────


class _NoLLMCfg:
    llm_provider = "anthropic"
    llm_role_chain_provider = "gateway"
    llm_fallback_provider = "openai,vertex_gemini,groq"
    anthropic_api_key = ""
    openai_api_key = ""
    groq_api_key = ""
    openrouter_api_key = ""
    vertex_project = ""
    vertex_base_url = ""
    llm_gateway_base_url = ""
    llm_noop_canned_text = "x"
    llm_trial_token_budget = 0


def _preflight_req(config):
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config, settings_service=None)))


@pytest.mark.asyncio
async def test_llm_preflight_blocks_with_no_provider_and_no_trial():
    from fastapi import HTTPException

    from devai.pipeline.routes import _llm_preflight

    with pytest.raises(HTTPException) as ei:
        await _llm_preflight(_preflight_req(_NoLLMCfg()), None)
    assert ei.value.status_code == 400
    assert "LLM provider" in ei.value.detail


@pytest.mark.asyncio
async def test_llm_preflight_allows_with_trial_budget():
    from devai.pipeline.routes import _llm_preflight

    cfg = _NoLLMCfg()
    cfg.llm_trial_token_budget = 5000  # platform chain serves trial users
    await _llm_preflight(_preflight_req(cfg), None)  # must not raise


@pytest.mark.asyncio
async def test_llm_preflight_skips_without_app_config():
    from types import SimpleNamespace

    from devai.pipeline.routes import _llm_preflight

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    await _llm_preflight(req, None)  # no config → never blocks (tests/minimal deps)
