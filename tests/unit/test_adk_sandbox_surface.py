from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from devai.adk import Agent, Dataset, EvalSuite, Publisher, Rubric, SandboxClient
from devai.adk.client import AdkAPIError
from devai.adk.validation import validate_artifacts
from devai.cli.adk_commands import _builder_for, adk_app
from devai.registry import RegistryClient


def test_agent_can_publish_safe_sandbox_defaults() -> None:
    body = (
        Agent("release-writer")
        .sandbox(
            default_mode="mock",
            tool_modes={"scm_merge": "block"},
            dataset=("release-smoke", "3"),
            max_tokens=500,
            max_cost_usd=0.25,
            max_wall_clock_s=30,
            ttl_seconds=600,
        )
        .to_dict()
    )

    assert body["sandbox"] == {
        "tools": {"default_mode": "mock", "overrides": {"scm_merge": "block"}},
        "limits": {"max_tokens": 500, "max_cost_usd": 0.25, "max_wall_clock_s": 30},
        "dataset": {"ref": "release-smoke", "version": "3"},
        "ttl_seconds": 600,
    }


def test_dataset_and_eval_suite_emit_registry_wire_bodies() -> None:
    dataset = (
        Dataset("release-smoke")
        .version("3")
        .description("Release note checks")
        .case("names version", "Summarise the release", contains=["v3"])
    )
    suite = EvalSuite("release-gate").version("2").dataset("release-smoke", "3").minimum_pass_rate(1.0)

    assert dataset.to_dict() == {
        "name": "release-smoke",
        "version": "3",
        "description": "Release note checks",
        "cases": [
            {
                "name": "names version",
                "input": "Summarise the release",
                "expect": {"contains": ["v3"]},
            }
        ],
    }
    assert suite.to_dict() == {
        "name": "release-gate",
        "version": "2",
        "description": "",
        "datasetRef": {"ref": "release-smoke", "version": "3"},
        "minimumPassRate": 1.0,
    }


def test_eval_suite_emits_extensible_scorers_and_structured_thresholds() -> None:
    suite = (
        EvalSuite("release-gate")
        .dataset("release-smoke", "3")
        .scorers("exact_match", "tool_trajectory", "latency", "cost")
        .thresholds(
            success=0.95,
            safety=1.0,
            hallucination=0.02,
            p95_latency_s=3,
            cost_per_run_usd=0.05,
        )
    )

    body = suite.to_dict()

    assert body["scorers"] == ["exact_match", "tool_trajectory", "latency", "cost"]
    assert body["thresholds"] == {
        "success": 0.95,
        "safety": 1.0,
        "hallucination": 0.02,
        "p95_latency_s": 3.0,
        "cost_per_run_usd": 0.05,
    }


def test_rubric_artifact_can_pin_a_provider_agnostic_judge_on_a_suite() -> None:
    rubric = (
        Rubric("support-quality")
        .version("3")
        .description("Human-calibrated support answer quality")
        .dimension("helpfulness", "The answer gives an actionable next step.")
        .dimension("groundedness", "Claims are supported by retrieved evidence.")
    )
    suite = (
        EvalSuite("release-gate")
        .dataset("release-smoke", "3")
        .scorers("exact_match", "llm_judge")
        .judge("anthropic", "claude-sonnet-4-20250514", rubric)
    )

    assert json.loads(rubric.to_dict()["content"])["dimensions"] == {
        "helpfulness": "The answer gives an actionable next step.",
        "groundedness": "Claims are supported by retrieved evidence.",
    }
    assert suite.to_dict()["judge"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "rubric": {
            "name": "support-quality",
            "version": "3",
            "dimensions": {
                "helpfulness": "The answer gives an actionable next step.",
                "groundedness": "Claims are supported by retrieved evidence.",
            },
        },
        "passThreshold": 0.7,
        "maxTokens": 800,
        "maxCostPerCaseUsd": 0.05,
        "timeoutSeconds": 30.0,
    }
    labelled = Dataset("calibration").case(
        "refund",
        "Can I get a refund?",
        human_scores={"helpfulness": 0.9, "groundedness": 0.8},
    )
    assert labelled.to_dict()["cases"][0]["humanScores"] == {"helpfulness": 0.9, "groundedness": 0.8}


def test_eval_suite_yaml_builder_preserves_scorers_and_thresholds() -> None:
    builder = _builder_for(
        {
            "kind": "EvalSuite",
            "metadata": {"name": "release-gate"},
            "spec": {
                "version": "2",
                "datasetRef": {"ref": "release-smoke", "version": "3"},
                "scorers": ["exact_match", "tool_trajectory", "latency", "cost"],
                "thresholds": {
                    "success": 0.95,
                    "safety": 1.0,
                    "hallucination": 0.02,
                    "p95_latency_s": 3,
                    "cost_per_run_usd": 0.05,
                },
            },
        }
    )

    assert builder.to_dict()["scorers"] == ["exact_match", "tool_trajectory", "latency", "cost"]
    assert builder.to_dict()["thresholds"] == {
        "success": 0.95,
        "safety": 1.0,
        "hallucination": 0.02,
        "p95_latency_s": 3.0,
        "cost_per_run_usd": 0.05,
    }


def test_publisher_routes_dataset_and_eval_suite_to_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, dict[str, Any]]] = []

    class Registry:
        def __init__(self, **_: Any) -> None:
            pass

        def publish_dataset(self, body: dict[str, Any]) -> dict[str, Any]:
            published.append(("dataset", body))
            return {}

        def publish_eval_suite(self, body: dict[str, Any]) -> dict[str, Any]:
            published.append(("eval-suite", body))
            return {}

        def publish_prompt(self, body: dict[str, Any]) -> dict[str, Any]:
            published.append(("rubric", body))
            return {}

    monkeypatch.setattr("devai.adk.publisher.RegistryClient", Registry)
    publisher = Publisher(registry_url="https://registry.example")

    assert publisher.publish(Dataset("smoke").case("works", "go")).ok is True
    assert publisher.publish(EvalSuite("gate").dataset("smoke", "1")).ok is True
    assert publisher.publish(Rubric("quality").dimension("helpfulness", "Useful answer")).ok is True
    assert [kind for kind, _ in published] == ["dataset", "eval-suite", "rubric"]


def test_registry_client_wraps_eval_artifacts_in_registry_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        requests.append((method, url, json.loads(kwargs["content"])))
        return httpx.Response(201, json={})

    monkeypatch.setattr(httpx, "request", request)
    client = RegistryClient(base_url="https://registry.example", namespace="tenant-a")

    client.publish_dataset(
        {
            "name": "release-smoke",
            "version": "3",
            "description": "Release note checks",
            "cases": [],
        }
    )
    client.publish_eval_suite(
        {
            "name": "release-gate",
            "version": "2",
            "datasetRef": {"ref": "release-smoke", "version": "3"},
            "minimumPassRate": 1.0,
        }
    )

    assert requests == [
        (
            "POST",
            "https://registry.example/v0/datasets",
            {
                "apiVersion": "registry.agentic.dev/v1alpha1",
                "kind": "Dataset",
                "metadata": {"name": "release-smoke", "namespace": "tenant-a", "tag": "3"},
                "spec": {"description": "Release note checks", "cases": []},
            },
        ),
        (
            "POST",
            "https://registry.example/v0/evalsuites",
            {
                "apiVersion": "registry.agentic.dev/v1alpha1",
                "kind": "EvalSuite",
                "metadata": {"name": "release-gate", "namespace": "tenant-a", "tag": "2"},
                "spec": {
                    "datasetRef": {"ref": "release-smoke", "version": "3"},
                    "minimumPassRate": 1.0,
                },
            },
        ),
    ]
    assert all("tenantId" not in body["metadata"] for _, _, body in requests)


def _write_artifact(root: Path, subdir: str, name: str, kind: str, spec: dict[str, Any]) -> Path:
    path = root / subdir / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"apiVersion": "registry.solo.io/v1alpha1", "kind": kind, "metadata": {"name": name}, "spec": spec}
        ),
        encoding="utf-8",
    )
    return path


def test_deep_validation_resolves_every_local_reference(tmp_path: Path) -> None:
    _write_artifact(tmp_path, "tools", "compile", "Tool", {"version": "1"})
    _write_artifact(tmp_path, "skills", "review", "Skill", {"version": "1", "tools": ["compile"]})
    _write_artifact(tmp_path, "prompts", "review-prompt", "Prompt", {"version": "2", "template": "Review."})
    _write_artifact(tmp_path, "mcp-servers", "scm", "MCPServer", {"version": "1", "tools": []})
    _write_artifact(tmp_path, "datasets", "smoke", "Dataset", {"version": "1", "cases": []})
    _write_artifact(
        tmp_path,
        "agents",
        "reviewer",
        "Agent",
        {
            "version": "1",
            "skill": "review",
            "promptRef": "review-prompt",
            "mcpServers": ["scm"],
            "sandbox": {"dataset": {"ref": "smoke", "version": "1"}},
            "llm": {"provider": "anthropic", "model": "claude"},
        },
    )

    assert validate_artifacts(list(tmp_path.rglob("*.yaml")), deep=True) == []


def test_deep_validation_names_unresolved_references(tmp_path: Path) -> None:
    agent = _write_artifact(
        tmp_path,
        "agents",
        "reviewer",
        "Agent",
        {
            "version": "1",
            "skill": "missing-skill",
            "promptRef": "missing-prompt",
            "mcpServers": ["missing-mcp"],
            "sandbox": {"dataset": {"ref": "missing-dataset", "version": "1"}},
            "llm": {"provider": "anthropic", "model": "claude"},
        },
    )

    failures = validate_artifacts([agent], deep=True)

    assert {failure.reference for failure in failures} == {
        "Skill/missing-skill",
        "Prompt/missing-prompt",
        "MCPServer/missing-mcp",
        "Dataset/missing-dataset@1",
    }


def test_catalog_roots_resolve_targets_without_validating_unrelated_artifacts(tmp_path: Path) -> None:
    target = _write_artifact(
        tmp_path / "target",
        "agents",
        "reviewer",
        "Agent",
        {"version": "1", "skill": "review", "llm": {"provider": "anthropic", "model": "claude"}},
    )
    _write_artifact(tmp_path / "catalog", "skills", "review", "Skill", {"version": "1", "tools": ["missing"]})

    assert validate_artifacts([target], deep=True, catalog_roots=[tmp_path / "catalog"]) == []


def test_sandbox_client_uses_session_cookie_and_never_sends_tenant_identity() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "sbx-1", "status": "ready"})

    client = SandboxClient(
        base_url="https://devai.example",
        session_cookie="encrypted-session",
        transport=httpx.MockTransport(handle),
    )
    result = client.create(
        {
            "agent": {"name": "reviewer", "version": "1"},
            "model": {"provider": "anthropic", "model": "claude"},
        }
    )

    assert result["id"] == "sbx-1"
    assert seen[0].headers["cookie"] == "devai_session=encrypted-session"
    assert "x-forwarded-user" not in seen[0].headers
    assert "x-forwarded-tenant" not in seen[0].headers


def test_sandbox_client_covers_lifecycle_and_eval_surface() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(200, json={"destroyed": "sbx-1"})
        if request.url.path.endswith("/traces"):
            return httpx.Response(200, json=[{"id": "inv-1"}])
        if request.url.path.endswith("/evals"):
            return httpx.Response(200, json={"summary": {"passed": 1, "failed": 0, "pass_rate": 1.0}})
        if request.url.path == "/api/evaluations":
            return httpx.Response(201, json={"id": "eval-durable", "summary": {"passed": 1, "failed": 0}})
        return httpx.Response(200, json={"id": "inv-1", "final_text": "done"})

    client = SandboxClient(base_url="https://devai.example", transport=httpx.MockTransport(handle))

    assert client.invoke("sbx-1", "go")["final_text"] == "done"
    assert client.traces("sbx-1") == [{"id": "inv-1"}]
    assert client.test("sbx-1", [{"name": "works", "input": "go", "expect": {}}])["summary"]["passed"] == 1
    assert client.evaluate("sbx-1", "release-gate", "2")["id"] == "eval-durable"
    assert client.destroy("sbx-1") == {"destroyed": "sbx-1"}
    assert calls == [
        ("POST", "/api/sandboxes/sbx-1/invoke", {"message": "go"}),
        ("GET", "/api/sandboxes/sbx-1/traces", None),
        ("POST", "/api/sandboxes/sbx-1/evals", {"cases": [{"name": "works", "input": "go", "expect": {}}]}),
        (
            "POST",
            "/api/evaluations",
            {"suite": {"name": "release-gate", "version": "2"}, "sandbox_id": "sbx-1"},
        ),
        ("DELETE", "/api/sandboxes/sbx-1", None),
    ]


def test_sandbox_client_publishes_agent_through_devai_gate_headers() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"name": "reviewer", "gate": {"status": "overridden"}})

    client = SandboxClient(base_url="https://devai.example", transport=httpx.MockTransport(handle))
    result = client.publish_agent(
        {"kind": "Agent", "metadata": {"name": "reviewer"}, "spec": {}},
        overwrite=True,
        override_reason="Approved judge outage",
    )

    assert result["gate"]["status"] == "overridden"
    assert seen[0].url.path == "/api/registry/agents"
    assert seen[0].url.query == b"overwrite=true"
    assert seen[0].headers["x-devai-eval-gate-override"] == "true"
    assert seen[0].headers["x-devai-eval-gate-override-reason"] == "Approved judge outage"


def test_sandbox_client_returns_a_typed_redacted_error() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Authorization: Bearer sk-secret-value"})

    client = SandboxClient(base_url="https://devai.example", transport=httpx.MockTransport(handle))

    with pytest.raises(AdkAPIError) as caught:
        client.invoke("sbx-1", "go")
    assert caught.value.status_code == 422
    assert "sk-secret-value" not in str(caught.value)


class _CLIClient:
    created: dict[str, Any] | None = None
    published: dict[str, Any] | None = None

    def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.created = spec
        return {"id": "sbx-cli", "status": "ready"}

    def invoke(self, sandbox_id: str, message: str) -> dict[str, Any]:
        return {"id": "inv-cli", "sandbox_id": sandbox_id, "final_text": message.upper()}

    def traces(self, sandbox_id: str) -> list[dict[str, Any]]:
        return [{"id": "inv-cli", "sandbox_id": sandbox_id}]

    def destroy(self, sandbox_id: str) -> dict[str, Any]:
        return {"destroyed": sandbox_id}

    def test(self, sandbox_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        del sandbox_id, cases
        return {
            "summary": {"cases": 2, "passed": 1, "failed": 1, "pass_rate": 0.5, "cost_usd": 0.02, "p95_latency_ms": 40},
            "results": [{"name": "bad", "passed": False, "failures": ["missing expected text: 'ok'"]}],
        }

    def evaluate(self, sandbox_id: str, suite_name: str, suite_version: str) -> dict[str, Any]:
        del sandbox_id, suite_name, suite_version
        return {
            "id": "eval-durable",
            "summary": {
                "cases": 2,
                "passed": 1,
                "failed": 1,
                "pass_rate": 0.5,
                "cost_usd": 0.02,
                "p95_latency_ms": 40,
            },
            "results": [{"name": "bad", "passed": False, "failures": ["missing expected text: 'ok'"]}],
        }

    def publish_agent(
        self,
        manifest: dict[str, Any],
        *,
        overwrite: bool = False,
        override_reason: str = "",
    ) -> dict[str, Any]:
        del overwrite, override_reason
        self.published = manifest
        return {"name": manifest["metadata"]["name"], "gate": {"status": "passed"}}

    def close(self) -> None:
        return None


def test_cli_sandbox_create_builds_a_draft_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _write_artifact(
        tmp_path,
        "agents",
        "reviewer",
        "Agent",
        {
            "version": "7",
            "promptRef": "review-prompt",
            "llm": {"provider": "anthropic", "model": "claude"},
            "sandbox": {"tools": {"default_mode": "block", "overrides": {}}},
        },
    )
    fake = _CLIClient()
    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: fake)

    result = CliRunner().invoke(
        adk_app,
        [
            "sandbox",
            "create",
            str(agent),
            "--llm-connector",
            "personal-anthropic",
            "--confirm-llm-connector",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == "sbx-cli"
    assert fake.created is not None
    assert fake.created["agent"] == {"name": "reviewer", "version": "7"}
    assert fake.created["credentials"] == {"llm_connector": "personal-anthropic", "confirmed": True}
    assert fake.created["draft"]["metadata"]["name"] == "reviewer"


def test_cli_agent_publish_uses_owner_scoped_api_and_attaches_eval_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _write_artifact(
        tmp_path,
        "agents",
        "reviewer",
        "Agent",
        {
            "title": "Reviewer",
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
            "systemPrompt": "Review the supplied diff.",
            "limits": {"maxTurns": 8, "timeoutSeconds": 900},
            "riskLevel": "medium",
            "evalSuite": {"ref": "release-gate", "version": "2"},
        },
    )
    fake = _CLIClient()
    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: fake)

    result = CliRunner().invoke(
        adk_app,
        ["publish", str(agent), "--eval-run-id", "eval-durable"],
    )

    assert result.exit_code == 0, result.output
    assert fake.published is not None
    assert fake.published["metadata"]["annotations"] == {"devai.tesserix.app/eval-run-id": "eval-durable"}


def test_cli_adk_test_exits_nonzero_and_names_failed_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = _write_artifact(
        tmp_path,
        "datasets",
        "smoke",
        "Dataset",
        {"version": "1", "cases": [{"name": "bad", "input": "go", "expect": {"contains": ["ok"]}}]},
    )
    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: _CLIClient())

    result = CliRunner().invoke(
        adk_app,
        ["test", "reviewer", "--sandbox-id", "sbx-cli", "--dataset", str(dataset)],
    )

    assert result.exit_code == 2
    assert "bad" in result.output
    assert "50.0%" in result.output


def test_cli_adk_test_resolves_suite_dataset_and_applies_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_artifact(
        tmp_path,
        "datasets",
        "smoke",
        "Dataset",
        {"version": "3", "cases": [{"name": "bad", "input": "go", "expect": {"contains": ["ok"]}}]},
    )
    suite = _write_artifact(
        tmp_path,
        "eval-suites",
        "release-gate",
        "EvalSuite",
        {
            "version": "1",
            "datasetRef": {"ref": "smoke", "version": "3"},
            "minimumPassRate": 0.5,
        },
    )
    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: _CLIClient())

    result = CliRunner().invoke(
        adk_app,
        ["test", "reviewer", "--sandbox-id", "sbx-cli", "--suite", str(suite)],
    )

    assert result.exit_code == 0, result.output
    assert "bad" in result.output
    assert "50.0%" in result.output
    assert "eval-durable" in result.output


def test_cli_adk_test_uses_inline_agent_evals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _write_artifact(
        tmp_path,
        "agents",
        "reviewer",
        "Agent",
        {
            "version": "1",
            "llm": {"provider": "anthropic", "model": "claude"},
            "evals": [{"name": "bad", "input": "go", "expect": {"contains": ["ok"]}}],
        },
    )
    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: _CLIClient())

    result = CliRunner().invoke(adk_app, ["test", str(agent), "--sandbox-id", "sbx-cli"])

    assert result.exit_code == 2
    assert "reviewer evaluation" in result.output
    assert "bad" in result.output


def test_cli_redacts_sandbox_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient(_CLIClient):
        def invoke(self, sandbox_id: str, message: str) -> dict[str, Any]:
            del sandbox_id, message
            raise AdkAPIError(401, "Authorization: Bearer sk-user-secret")

    monkeypatch.setattr("devai.cli.adk_commands._new_sandbox_client", lambda **_: FailingClient())

    result = CliRunner().invoke(adk_app, ["sandbox", "invoke", "sbx-cli", "go"])

    assert result.exit_code == 2
    assert "401" in result.output
    assert "sk-user-secret" not in result.output
