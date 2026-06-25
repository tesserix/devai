"""CI ground truth + failure forensics — shared by stage gates and agent tools.

One source of truth for "is this branch ACTUALLY green?" and "what exactly
failed?". The pipeline's monitor/test gates verify through this, and agents
(ci_monitor, qa_tester, recovery) call the same logic via the scm_ci_* tools
— so every debug round works from the repo's real workflow state and real
failure logs, never narration.

GitHub-specific today (uses the SCM client's raw `_request`); other SCM
providers return 'unknown' and callers keep their own verdict.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def latest_ci_conclusions(scm: Any, repo: str, branch: str) -> dict[str, Any]:
    """Verdict for the newest run of EVERY workflow on ``branch``.

    Returns {"verdict", "url", "runs": [{workflow, conclusion, status, url}]}.
    verdict: 'success' | 'failure'/'cancelled'/… (first red workflow's
    conclusion) | 'in_progress' | 'none' | 'unknown'.
    """
    req = getattr(scm, "_request", None)
    if req is None or not repo or not branch:
        return {"verdict": "unknown", "url": "", "runs": []}
    try:
        resp = await req("GET", f"/repos/{repo}/actions/runs", params={"branch": branch, "per_page": "10"})
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return {"verdict": "none", "url": "", "runs": []}
        newest_per_wf: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for r in runs:
            wf = r.get("workflow_id") or r.get("name")
            if wf in seen:
                continue
            seen.add(wf)
            newest_per_wf.append(
                {
                    "workflow": r.get("name", ""),
                    "status": r.get("status", ""),
                    "conclusion": r.get("conclusion"),
                    "url": r.get("html_url", ""),
                    "run_id": r.get("id"),
                    "head_sha": (r.get("head_sha") or "")[:10],
                }
            )
        if runs[0].get("status") != "completed":
            return {"verdict": "in_progress", "url": runs[0].get("html_url", ""), "runs": newest_per_wf}
        for r in newest_per_wf:
            if r["status"] == "completed" and r["conclusion"] != "success":
                return {"verdict": str(r["conclusion"] or "failure"), "url": r["url"], "runs": newest_per_wf}
        return {"verdict": "success", "url": runs[0].get("html_url", ""), "runs": newest_per_wf}
    except Exception:  # noqa: BLE001
        logger.debug("ci_insight: conclusions failed for %s@%s", repo, branch, exc_info=True)
        return {"verdict": "unknown", "url": "", "runs": []}


async def failed_job_logs(scm: Any, repo: str, run_ref: str | int) -> str:
    """Failed jobs' names, failed steps and a REDACTED log tail for a run.

    ``run_ref`` is a run id or an html url ending in one. Best-effort: any
    API hiccup returns "" rather than raising — forensics must never break
    the loop that needs them.
    """
    req = getattr(scm, "_request", None)
    run_id = str(run_ref).rstrip("/").rsplit("/", 1)[-1]
    if req is None or not run_id.isdigit():
        return ""
    try:
        resp = await req("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs", params={"per_page": "20"})
        jobs = resp.json().get("jobs", [])
        failed = [j for j in jobs if j.get("conclusion") not in (None, "success", "skipped")]
        if not failed:
            return ""
        lines: list[str] = []
        for job in failed[:3]:
            steps = [
                f"step '{s.get('name')}' → {s.get('conclusion')}"
                for s in job.get("steps", [])
                if s.get("conclusion") not in (None, "success", "skipped")
            ]
            lines.append(f"job '{job.get('name')}' → {job.get('conclusion')}; " + "; ".join(steps[:5]))
            try:
                log_resp = await req("GET", f"/repos/{repo}/actions/jobs/{job.get('id')}/logs")
                text = log_resp.text if hasattr(log_resp, "text") else ""
                if text:
                    from devai.services.redact import redact_secrets

                    lines.append(redact_secrets(text[-2500:]))
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(lines)[:6000]
    except Exception:  # noqa: BLE001
        logger.debug("ci_insight: failed-job logs failed for %s run %s", repo, run_id, exc_info=True)
        return ""


async def rerun_failed_jobs(scm: Any, repo: str, run_id: int | str) -> dict[str, Any]:
    """Re-run only the FAILED jobs of a workflow run (re-test after a fix
    that didn't change code, e.g. a flake check or workflow-file repair on
    another branch). Returns {"ok", "detail"}."""
    req = getattr(scm, "_request", None)
    if req is None:
        return {"ok": False, "detail": "SCM provider does not support workflow reruns"}
    try:
        resp = await req("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
        ok = getattr(resp, "status_code", 0) in (201, 202)
        return {"ok": ok, "detail": "re-run requested" if ok else f"HTTP {getattr(resp, 'status_code', '?')}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200]}


async def repo_has_workflows(scm: Any, repo: str, branch: str = "") -> bool:
    req = getattr(scm, "_request", None)
    if req is None:
        return False
    try:
        resp = await req("GET", f"/repos/{repo}/contents/.github/workflows", params={"ref": branch} if branch else None)
        return resp.status_code == 200 and bool(resp.json())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["failed_job_logs", "latest_ci_conclusions", "repo_has_workflows", "rerun_failed_jobs"]
