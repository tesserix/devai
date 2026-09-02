"""ADK runner Job spec — the dispatch contract agents-as-Jobs rely on."""

from __future__ import annotations

import json

from devai.runtime.job_spec import RunnerJobInputs, build_job_spec
from devai.runtime.k8s_client import RuntimeConfig


def _cfg() -> RuntimeConfig:
    return RuntimeConfig(
        namespace="devai",
        runner_image="example.dev/devai-runner:test",
        runner_image_per_stack={},
        preview_domain="example.app",
        preview_namespace="devai-previews",
        registry_url="http://aregistry:12121",
        pull_secret_name=None,
        service_account_name="devai-runner",
        default_ttl_seconds=3600,
        default_backoff_limit=0,
        pod_security_context={},
    )


def _inputs(**kw) -> RunnerJobInputs:
    base = dict(
        task_id="t-123",
        stage_name="scaffold_app",
        agent_name="senior_developer",
        image="example.dev/devai-runner:test",
        repo="tesserix/demo",
        intent="build the thing",
        blueprint="alm-pipeline",
        extra_env={},
        triggered_by="a@example.com",
        trace_id="trace-1",
    )
    base.update(kw)
    return RunnerJobInputs(**base)


def _env_of(spec: dict) -> dict[str, dict]:
    container = spec["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in container["env"]}


def test_job_spec_core_contract():
    spec = build_job_spec(_cfg(), _inputs())
    pod = spec["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "devai-runner"  # WI-bound identity
    env = _env_of(spec)
    assert env["DEVAI_RUNNER_AGENT"]["value"] == "senior_developer"
    assert env["DEVAI_TRIGGERED_BY"]["value"] == "a@example.com"
    assert env["DEVAI_TRACE_ID"]["value"] == "trace-1"
    # Secrets ride as optional refs — a missing key never blocks scheduling.
    assert env["DEVAI_ANTHROPIC_API_KEY"]["valueFrom"]["secretKeyRef"]["optional"] is True


def test_job_spec_carries_keyless_llm_config_from_dispatcher_env(monkeypatch):
    monkeypatch.setenv("DEVAI_VERTEX_PROJECT", "proj-1")
    monkeypatch.setenv("DEVAI_VERTEX_LOCATION", "asia-south1")
    monkeypatch.setenv("DEVAI_LLM_FALLBACK_PROVIDER", "vertex_gemini")
    monkeypatch.setenv("DEVAI_LLM_TIER_HEAVY", "anthropic:claude-sonnet-4-20250514")
    monkeypatch.setenv("DEVAI_LLM_GATEWAY_BASE_URL", "http://ai-gateway:8080")
    monkeypatch.setenv("DEVAI_LLM_GATEWAY_REQUIRED", "true")
    spec = build_job_spec(_cfg(), _inputs())
    env = _env_of(spec)
    assert env["DEVAI_VERTEX_PROJECT"]["value"] == "proj-1"
    assert env["DEVAI_VERTEX_LOCATION"]["value"] == "asia-south1"
    assert env["DEVAI_LLM_TIER_HEAVY"]["value"] == "anthropic:claude-sonnet-4-20250514"
    assert env["DEVAI_LLM_GATEWAY_BASE_URL"]["value"] == "http://ai-gateway:8080"
    assert env["DEVAI_LLM_GATEWAY_REQUIRED"]["value"] == "true"


def test_job_spec_omits_unset_passthrough_vars(monkeypatch):
    monkeypatch.delenv("DEVAI_VERTEX_PROJECT", raising=False)
    spec = build_job_spec(_cfg(), _inputs())
    assert "DEVAI_VERTEX_PROJECT" not in _env_of(spec)


def test_job_spec_propagates_runner_telemetry_without_copying_secrets(monkeypatch):
    monkeypatch.setenv("DEVAI_TELEMETRY_PROVIDER", "langfuse")
    monkeypatch.setenv("DEVAI_LANGFUSE_BASE_URL", "http://langfuse.observability:3000")
    monkeypatch.setenv("DEVAI_LANGFUSE_PUBLIC_URL", "https://langfuse.example")
    monkeypatch.setenv("DEVAI_OTEL_ENDPOINT", "http://otel-gateway:4318")
    monkeypatch.setenv("DEVAI_OTEL_SERVICE_NAMESPACE", "devai-prod")
    monkeypatch.setenv("DEVAI_OTEL_EXPORT_INTERVAL_MS", "5000")
    monkeypatch.setenv("DEVAI_METRICS_ENABLED", "true")
    monkeypatch.setenv("DEVAI_LANGFUSE_PUBLIC_KEY", "must-not-be-copied")
    monkeypatch.setenv("DEVAI_LANGFUSE_SECRET_KEY", "must-not-be-copied")

    env = _env_of(build_job_spec(_cfg(), _inputs()))

    assert env["DEVAI_TELEMETRY_PROVIDER"]["value"] == "langfuse"
    assert env["DEVAI_LANGFUSE_BASE_URL"]["value"] == "http://langfuse.observability:3000"
    assert env["DEVAI_LANGFUSE_PUBLIC_URL"]["value"] == "https://langfuse.example"
    assert env["DEVAI_OTEL_ENDPOINT"]["value"] == "http://otel-gateway:4318"
    assert env["DEVAI_OTEL_SERVICE_NAME"]["value"] == "devai-runner"
    assert env["DEVAI_OTEL_SERVICE_NAMESPACE"]["value"] == "devai-prod"
    assert env["DEVAI_OTEL_EXPORT_INTERVAL_MS"]["value"] == "5000"
    assert env["DEVAI_METRICS_ENABLED"]["value"] == "true"
    for key in ("DEVAI_LANGFUSE_PUBLIC_KEY", "DEVAI_LANGFUSE_SECRET_KEY"):
        assert "value" not in env[key]
        assert env[key]["valueFrom"]["secretKeyRef"] == {
            "name": "devai-langfuse-secrets",
            "key": key,
            "optional": True,
        }


def test_job_spec_embeds_agent_profile_for_audit():
    profile = {
        "image": "x",
        "model_provider": "vertex_gemini",
        "model_name": "gemini-2.5-flash",
        "digest": "sha256:" + "a" * 64,
    }
    spec = build_job_spec(_cfg(), _inputs(agent_profile=profile))
    env = _env_of(spec)
    assert json.loads(env["DEVAI_AGENT_PROFILE"]["value"]) == profile
    assert spec["metadata"]["annotations"]["devai.tesserix.app/composition-digest"] == profile["digest"]
    assert (
        spec["spec"]["template"]["metadata"]["annotations"]["devai.tesserix.app/composition-digest"]
        == profile["digest"]
    )
