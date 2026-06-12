"""update_governance — CLAUDE.md maintenance on the working branch."""

from __future__ import annotations

import pytest

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.governance import _UpdateGovernanceStage
from devai.pipeline.types import DevAITask


class _Cfg:
    pipeline_label = "x"


class _SCM:
    def __init__(self, existing="# Claude Reference Guide\n\n## Critical Rules\n- rule one"):
        self.existing = existing
        self.commits = []

    async def get_file_content(self, repo, path, ref=None):
        if path == "CLAUDE.md":
            return self.existing
        if path == "package.json":
            return '{"scripts": {"dev": "next dev", "test": "vitest"}}'
        raise FileNotFoundError(path)

    async def list_files(self, repo, path="", ref=None):
        return [{"name": "src", "type": "dir"}, {"name": "package.json", "type": "file"}]

    async def create_or_update_file(self, repo, path, content, message, branch, sha=None):
        self.commits.append({"path": path, "content": content, "branch": branch})
        return {}


@pytest.mark.asyncio
async def test_mechanical_compose_preserves_rules_and_adds_facts():
    scm = _SCM()
    deps = StageDeps(config=_Cfg(), scm=scm)  # no LLM → mechanical merge
    stage = _UpdateGovernanceStage(deps, {})
    task = DevAITask(intent="build the petstore", blueprint="b", repo="o/r")
    task.branch_name = "devai/work"
    task.agent_context["detected_tech_stack"] = "Next.js 15 + TypeScript"
    task.agent_context["technical_plan"] = "App router, Prisma, Vitest"

    result = await stage.execute(task)

    assert result.data["governance_updated"] is True
    commit = scm.commits[0]
    assert commit["path"] == "CLAUDE.md" and commit["branch"] == "devai/work"
    body = commit["content"]
    assert "## Critical Rules" in body and "rule one" in body  # preserved
    assert "Next.js 15" in body and "npm run dev" in body  # learned facts
    assert "## Architecture Decisions" in body and "Prisma" in body


@pytest.mark.asyncio
async def test_no_scm_skips_visibly():
    deps = StageDeps(config=_Cfg())
    stage = _UpdateGovernanceStage(deps, {})
    task = DevAITask(intent="x", blueprint="b", repo="o/r")
    result = await stage.execute(task)
    assert result.data["governance_updated"] is False
