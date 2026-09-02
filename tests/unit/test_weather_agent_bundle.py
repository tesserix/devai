from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse, ToolCall
from devai.adk.validation import validate_artifacts
from devai.agentruntime.tesserix import TesserixSpecRuntime
from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import load_blueprint
from devai.blueprint.registry import StageRegistry, register_defaults
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask
from devai.runtime.job_watcher import JobOutcome
from devai.runtime.k8s_client import RuntimeConfig
from devai.specializations.loader import load_specialization
from devai.tools import registry as tool_registry
from devai.tools.dispatch import ToolDispatcher

ROOT = Path(__file__).parents[2]
SEEDS = ROOT / "architecture/registry-seeds"
SPECIALIZATION = ROOT / "specializations/specialists/weather.yaml"
BLUEPRINT = ROOT / "blueprints/weather-agent.yaml"


def _load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class _WeatherLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.responses = [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="weather-1",
                        name="weather-current",
                        arguments={"location": "Melbourne", "unit": "celsius"},
                    )
                ],
                finish_reason="tool_use",
            ),
            LLMResponse(
                text=(
                    '{"location":"Melbourne","available":true,"temperature":18.0,'
                    '"unit":"celsius","condition":"partly cloudy",'
                    '"observed_at":"2026-08-29T00:00:00Z",'
                    '"source":"deterministic-fixture","summary":"Melbourne is partly cloudy at 18 C."}'
                ),
                finish_reason="stop",
            ),
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class _WeatherTools(ToolDispatcher):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        super().__init__()

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return await super().execute(name, arguments)


class _JobRuntime:
    def __init__(self) -> None:
        self.config = RuntimeConfig.from_settings(Settings())
        self.created: list[dict[str, Any]] = []

    async def create_job(self, job: dict[str, Any]) -> str:
        self.created.append(job)
        return str(job["metadata"]["name"])


class _JobWatcher:
    def __init__(self) -> None:
        self.job_name = ""

    def register(self, job_name: str) -> asyncio.Event:
        self.job_name = job_name
        event = asyncio.Event()
        event.set()
        return event

    def consume(self, job_name: str) -> JobOutcome | None:
        assert job_name == self.job_name
        return JobOutcome(
            job_name=job_name,
            succeeded=True,
            message="Weather Job completed",
            logs_tail='RESULT::{"location":"Melbourne","available":true}',
        )


def test_weather_bundle_resolves_the_exact_skill_prompt_tool_and_eval_contract() -> None:
    paths = {
        "agent": SEEDS / "agents/weather-agent.yaml",
        "skill": SEEDS / "skills/weather.yaml",
        "prompt": SEEDS / "prompts/weather-prompt-v1.yaml",
        "tool": SEEDS / "tools/weather-current.yaml",
        "dataset": SEEDS / "datasets/weather-golden.yaml",
        "suite": SEEDS / "eval-suites/weather-golden-suite.yaml",
    }
    documents = {name: _load(path) for name, path in paths.items()}
    specialization = load_specialization(SPECIALIZATION)
    runtime_tool = tool_registry.get("weather-current")

    assert specialization.allowed_tools == ["weather-current"]
    assert documents["agent"]["metadata"]["tag"] == "1.0.0"
    assert documents["agent"]["spec"]["skills"] == ["weather"]
    assert documents["agent"]["spec"]["prompts"] == ["weather-prompt-v1"]
    assert documents["agent"]["spec"]["tools"] == ["weather-current"]
    assert documents["agent"]["spec"]["evalSuite"] == {
        "ref": "weather-golden-suite",
        "version": "1",
    }
    assert documents["skill"]["spec"]["tools"] == ["weather-current"]
    assert documents["prompt"]["spec"]["systemPrompt"] == specialization.system_prompt
    assert runtime_tool is not None
    assert documents["tool"]["spec"]["inputSchema"] == runtime_tool.spec.parameters
    assert documents["suite"]["spec"]["datasetRef"] == {"ref": "weather-golden", "version": "1"}
    assert documents["suite"]["spec"]["minimumPassRate"] == 1.0
    assert validate_artifacts(paths.values(), deep=True, catalog_roots=[SEEDS]) == []


def test_weather_blueprint_runs_the_registry_agent_as_an_isolated_job() -> None:
    blueprint = load_blueprint(BLUEPRINT)
    source = _load(BLUEPRINT)
    registry_blueprint = _load(SEEDS / "blueprints/weather-agent.yaml")

    assert blueprint.name == "weather-agent"
    assert len(blueprint.stages) == 1
    assert blueprint.stages[0].stage == "run_as_job"
    assert blueprint.stages[0].resolved_agent() == "weather"
    assert blueprint.metadata["registry_publish"] is True
    assert registry_blueprint["spec"]["stages"] == source["stages"]
    assert registry_blueprint["spec"]["nodes"][0]["ref"] == "weather-agent"


async def test_weather_agent_runs_through_tesserix_adk_and_the_real_tool_boundary() -> None:
    llm = _WeatherLLM()
    tools = _WeatherTools()
    runtime = TesserixSpecRuntime(llm=llm, dispatcher=tools)

    result = await runtime.run(
        load_specialization(SPECIALIZATION),
        DevAITask(intent="What is the current weather in Melbourne?", triggered_by="learner@example.com"),
        system_prompt="Use the reviewed Weather tool.",
        user_prompt="What is the current weather in Melbourne?",
    )

    assert tools.calls == [("weather-current", {"location": "Melbourne", "unit": "celsius"})]
    assert result.patch["location"] == "Melbourne"
    assert result.patch["available"] is True
    assert result.patch["temperature"] == 18.0
    assert len(llm.requests) == 2
    assert result.tool_calls == 1


async def test_weather_blueprint_executes_through_the_kubernetes_job_boundary() -> None:
    runtime = _JobRuntime()
    watcher = _JobWatcher()
    registry = StageRegistry()
    register_defaults(registry)
    deps = StageDeps(
        config=Settings(),
        extra={"k8s_runtime": runtime, "job_watcher": watcher},
    )

    result = await BlueprintExecutor(registry, deps).execute(
        load_blueprint(BLUEPRINT),
        DevAITask(
            id="weather-run-1",
            intent="What is the current weather in Melbourne?",
            blueprint="weather-agent",
            triggered_by="learner@example.com",
        ),
    )

    assert result.stages_completed == ["answer-weather"]
    assert len(runtime.created) == 1
    container = runtime.created[0]["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    assert environment["DEVAI_RUNNER_AGENT"] == "weather"
