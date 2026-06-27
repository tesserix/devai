"""Autonomous backlog watcher (Phase 1).

Proves the watcher polls onboarded repos, dispatches a run for each NEW issue,
skips ones already in the process-once ledger, caps dispatches per repo per
pass, and resolves the watch-label filter — all without a live SCM / Redis /
pipeline (dependencies are injected)."""

from __future__ import annotations

import pytest

from devai.onboarding.watcher import IssueWatcher, _build_requirements


class _Repo:
    def __init__(self, full_name: str) -> None:
        self.full_name = full_name


class _Onboarding:
    def __init__(self, repos: list[_Repo]) -> None:
        self._repos = repos

    async def list_onboarded(self, state):  # noqa: ANN001
        return self._repos


class _SCM:
    def __init__(self, issues_by_repo: dict[str, list[dict]]) -> None:
        self._issues = issues_by_repo
        self.calls: list[tuple] = []

    async def list_issues(self, repo, state="open", labels=None, limit=100):  # noqa: ANN001
        self.calls.append((repo, tuple(labels or ())))
        return list(self._issues.get(repo, []))


class _Pipeline:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    async def dispatch(self, *, intent, repo, trigger_type, label, agent_context, principal):  # noqa: ANN001
        self.dispatched.append({"repo": repo, "ref": agent_context.get("trigger_ref"), "type": trigger_type})
        return f"run-{len(self.dispatched)}"


class _Redis:
    """Minimal async Redis: exists/set over a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def exists(self, key):  # noqa: ANN001
        return 1 if key in self.store else 0

    async def set(self, key, val, ex=None):  # noqa: ANN001
        self.store[key] = val
        return True


class _Config:
    pipeline_label = "devai:automate"
    issue_watch_interval_seconds = 300
    issue_watch_max_per_repo = 3
    issue_watch_labels = ""


def _issue(n: int, **kw) -> dict:
    return {"number": n, "title": f"issue {n}", "body": "do it", "labels": [], "user": {"login": "alice"}, **kw}


def _watcher(onboarding, scm, pipeline, redis=None, config=None) -> IssueWatcher:  # noqa: ANN001
    return IssueWatcher(onboarding=onboarding, scm=scm, pipeline=pipeline, redis=redis, config=config or _Config())


@pytest.mark.asyncio
async def test_dispatches_new_issues_and_marks_processed() -> None:
    scm = _SCM({"tesserix/x": [_issue(1), _issue(2)]})
    pipe = _Pipeline()
    redis = _Redis()
    w = _watcher(_Onboarding([_Repo("tesserix/x")]), scm, pipe, redis)

    report = await w.poll_once()

    assert report == {"repos": 1, "dispatched": 2}
    assert {d["ref"] for d in pipe.dispatched} == {"1", "2"}
    assert len(redis.store) == 2  # both recorded in the ledger
    # a second pass dispatches nothing — all processed
    assert (await w.poll_once())["dispatched"] == 0


@pytest.mark.asyncio
async def test_skips_already_processed_issue() -> None:
    scm = _SCM({"tesserix/x": [_issue(1), _issue(2)]})
    pipe = _Pipeline()
    redis = _Redis()
    redis.store[IssueWatcher._ledger_key("tesserix/x", 1)] = "1"  # #1 already seen
    w = _watcher(_Onboarding([_Repo("tesserix/x")]), scm, pipe, redis)

    await w.poll_once()

    assert {d["ref"] for d in pipe.dispatched} == {"2"}  # only the unseen one


@pytest.mark.asyncio
async def test_caps_dispatches_per_repo_oldest_first() -> None:
    scm = _SCM({"tesserix/x": [_issue(i) for i in range(10, 0, -1)]})  # 10..1 (unsorted)
    pipe = _Pipeline()
    w = _watcher(_Onboarding([_Repo("tesserix/x")]), scm, pipe, _Redis())  # default cap = 3

    report = await w.poll_once()

    assert report["dispatched"] == 3  # capped
    assert [d["ref"] for d in pipe.dispatched] == ["1", "2", "3"]  # oldest-first


def test_watch_label_resolution() -> None:
    cfg = _Config()
    w = _watcher(_Onboarding([]), _SCM({}), _Pipeline(), None, cfg)
    assert w._watch_labels() == ["devai:automate"]  # empty → pipeline_label
    cfg.issue_watch_labels = "*"
    assert w._watch_labels() is None  # all open issues
    cfg.issue_watch_labels = "bug, feature"
    assert w._watch_labels() == ["bug", "feature"]


def test_build_requirements_carries_title_body_labels() -> None:
    req = _build_requirements({"number": 7, "title": "Add auth", "body": "Use OAuth", "labels": [{"name": "feature"}]})
    assert "Issue #7" in req
    assert "Add auth" in req
    assert "Use OAuth" in req
    assert "feature" in req
