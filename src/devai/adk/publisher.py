"""Publish ADK-built entries to aregistry.

Acts as a tiny CI helper: takes a list of builder objects, walks the
correct kind-to-endpoint map, POSTs each, and returns a summary. The
publishing path goes through :class:`devai.registry.RegistryClient` so
the same idempotency rules (409 / 'duplicate version' → success) apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from devai.adk.builders import Agent, Dataset, EvalSuite, McpServer, Prompt, Rubric, Skill
from devai.registry import RegistryClient, RegistryError


@dataclass(slots=True)
class PublishResult:
    """Per-entry outcome of a publish batch."""

    kind: str
    name: str
    ok: bool
    status: str  # 'published' | 'exists' | 'failed'
    error: str = ""


@dataclass(slots=True)
class PublishSummary:
    results: list[PublishResult] = field(default_factory=list)

    def add(self, r: PublishResult) -> None:
        self.results.append(r)

    @property
    def failed(self) -> list[PublishResult]:
        return [r for r in self.results if not r.ok]

    def __str__(self) -> str:  # pragma: no cover — operational pretty-print
        ok = sum(1 for r in self.results if r.ok)
        bad = len(self.results) - ok
        lines = [f"published {ok} entries, {bad} failed"]
        for r in self.failed:
            lines.append(f"  - {r.kind}/{r.name}: {r.error}")
        return "\n".join(lines)


class Publisher:
    """Batch publisher for ADK builders.

    Constructs its own :class:`RegistryClient` (so callers don't have
    to import from ``devai.registry``). Idempotent: a re-publish of an
    unchanged entry is reported as ``exists`` rather than ``failed``.
    """

    def __init__(
        self,
        *,
        registry_url: str,
        token: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = RegistryClient(base_url=registry_url, token=token, timeout_seconds=timeout_seconds)
        self._summary = PublishSummary()

    def publish(self, builder: Skill | Prompt | McpServer | Agent | Dataset | EvalSuite | Rubric) -> PublishResult:
        body = builder.to_dict()
        name = builder.name
        try:
            kind = _publish_builder(self._client, builder, body)
            r = PublishResult(kind=kind, name=name, ok=True, status="published")
        except RegistryError as e:
            msg = str(e)
            if "duplicate version" in msg or "409" in msg:
                r = PublishResult(kind=kind, name=name, ok=True, status="exists")
            else:
                r = PublishResult(kind=kind, name=name, ok=False, status="failed", error=msg)
        self._summary.add(r)
        return r

    def publish_many(
        self,
        builders: list[Skill | Prompt | McpServer | Agent | Dataset | EvalSuite | Rubric],
    ) -> PublishSummary:
        for b in builders:
            self.publish(b)
        return self._summary

    def summary(self) -> PublishSummary:
        return self._summary


def _publish_builder(
    client: RegistryClient,
    builder: Skill | Prompt | McpServer | Agent | Dataset | EvalSuite | Rubric,
    body: dict[str, object],
) -> str:
    if isinstance(builder, Skill):
        client.publish_skill(body)
        return "skill"
    if isinstance(builder, Prompt):
        client.publish_prompt(body)
        return "prompt"
    if isinstance(builder, Rubric):
        client.publish_prompt(body)
        return "rubric"
    if isinstance(builder, McpServer):
        client.publish_mcp_server(body)
        return "mcp-server"
    if isinstance(builder, Agent):
        client.publish_agent(body)
        return "agent"
    if isinstance(builder, Dataset):
        client.publish_dataset(body)
        return "dataset"
    client.publish_eval_suite(body)
    return "eval-suite"
