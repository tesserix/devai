"""GitHub-backed, tenant-safe product feedback threads."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from fastapi import HTTPException

from devai.identity import Principal
from devai.scm import SCMClient

_FEEDBACK_TYPES = {"story": "user story", "bug": "bug report", "task": "task"}
_MANAGER_ROLES = frozenset({"admin", "platform-admin", "support", "support-engineer"})
_ISSUE_MARKER_RE = re.compile(r"<!-- devai-feedback: ([A-Za-z0-9_-]+) -->")
_REPLY_MARKER_RE = re.compile(r"<!-- devai-feedback-reply: ([A-Za-z0-9_-]+) -->")
_LEGACY_SUBMITTER_RE = re.compile(r"^Submitted by:\s*(.+?)\s*$", re.MULTILINE)


def _encoded_marker(prefix: str, values: dict[str, Any]) -> str:
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"<!-- {prefix}: {encoded} -->"


def _decode_last_marker(pattern: re.Pattern[str], body: str) -> dict[str, Any] | None:
    matches = pattern.findall(body or "")
    if not matches:
        return None
    try:
        encoded = matches[-1]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _owner_digest(principal: Principal) -> str:
    owner_scope = principal.user_scope_id or principal.email
    return hashlib.sha256(owner_scope.encode()).hexdigest()


def _labels(issue: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "") if isinstance(label, dict) else label) for label in issue.get("labels") or []}


def _kind(issue: dict[str, Any]) -> str:
    for label in _labels(issue):
        if label.startswith("type:") and label.removeprefix("type:") in _FEEDBACK_TYPES:
            return label.removeprefix("type:")
    match = re.match(r"^\[(story|bug|task)]", str(issue.get("title") or ""), flags=re.IGNORECASE)
    return match.group(1).lower() if match else "task"


def _title(issue: dict[str, Any]) -> str:
    return re.sub(r"^\[(?:story|bug|task)]\s*", "", str(issue.get("title") or ""), flags=re.IGNORECASE)


def _description(body: str) -> str:
    clean = _ISSUE_MARKER_RE.sub("", body or "").rstrip()
    clean = re.sub(r"^## [^\n]+\n+", "", clean)
    footer = re.search(r"\n\n---\nSubmitted by:.*$", clean, flags=re.DOTALL)
    return clean[: footer.start()].rstrip() if footer else clean.strip()


def _legacy_submitter(issue: dict[str, Any]) -> str:
    matches = _LEGACY_SUBMITTER_RE.findall(str(issue.get("body") or ""))
    return matches[-1].strip() if matches else ""


class FeedbackService:
    """Expose GitHub issues as user-owned support conversations.

    GitHub remains authoritative for issue state and comments. A hidden marker
    records a hash of the tenant-qualified identity, so access checks do not
    rely on a caller-controlled issue number or on labels alone.
    """

    def __init__(
        self,
        scm: SCMClient,
        *,
        repo: str,
        assignees: list[str] | None = None,
        support_identities: list[str] | None = None,
    ) -> None:
        self._scm = scm
        self._repo = repo
        self._assignees = {str(value).strip().lower() for value in assignees or [] if str(value).strip()}
        self._support_identities = {
            str(value).strip().lower() for value in support_identities or [] if str(value).strip()
        }

    def can_manage(self, principal: Principal) -> bool:
        roles = {str(role).strip().lower() for role in principal.roles or []}
        identities = {principal.uid.strip().lower(), principal.email.strip().lower()}
        managers = self._assignees | self._support_identities
        return bool(roles & _MANAGER_ROLES or (identities - {""}) & managers)

    def _owns(self, issue: dict[str, Any], principal: Principal) -> bool:
        metadata = _decode_last_marker(_ISSUE_MARKER_RE, str(issue.get("body") or ""))
        if metadata:
            return metadata.get("owner_digest") == _owner_digest(principal)
        submitted_by = _legacy_submitter(issue).casefold()
        return bool(submitted_by and submitted_by == principal.email.strip().casefold())

    def _authorize(self, issue: dict[str, Any], principal: Principal) -> None:
        if self.can_manage(principal) or self._owns(issue, principal):
            return
        raise HTTPException(status_code=404, detail="feedback thread not found")

    async def create(
        self,
        principal: Principal,
        *,
        kind: str,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        kind = kind.strip().lower()
        title = title.strip()
        description = description.strip()
        if kind not in _FEEDBACK_TYPES:
            raise HTTPException(status_code=422, detail="type must be story, bug, or task")
        if not title or len(title) > 200 or not description or len(description) > 10000:
            raise HTTPException(status_code=422, detail="title and description are required and bounded")

        submitter = principal.display_name or principal.email or principal.uid
        marker = _encoded_marker(
            "devai-feedback",
            {
                "owner_digest": _owner_digest(principal),
                "submitter": submitter,
                "submitter_email": principal.email,
                "v": 1,
            },
        )
        body = (
            f"## {_FEEDBACK_TYPES[kind].title()}\n\n{description}\n\n"
            f"---\nSubmitted by: {principal.email or principal.uid}\n"
            "This issue was created from the DevAI feedback support inbox.\n\n"
            f"{marker}"
        )
        issue = await self._scm.create_issue(
            self._repo,
            f"[{kind}] {title}",
            body,
            ["feedback", f"type:{kind}"],
        )
        number = issue.get("number")
        if not number:
            raise HTTPException(status_code=502, detail="feedback provider did not return an issue number")
        assign = getattr(self._scm, "assign_issue", None)
        if callable(assign) and self._assignees:
            await assign(self._repo, int(number), sorted(self._assignees))
        return self._thread(issue, principal, include_replies=False)

    async def list(self, principal: Principal) -> dict[str, Any]:
        manager = self.can_manage(principal)
        issues = await self._scm.list_issues(
            self._repo,
            state="all",
            labels=["feedback"],
            limit=500,
        )
        visible = [issue for issue in issues if manager or self._owns(issue, principal)]
        visible.sort(key=lambda issue: str(issue.get("updated_at") or issue.get("created_at") or ""), reverse=True)
        return {
            "threads": [self._thread(issue, principal, include_replies=False) for issue in visible],
            "can_manage": manager,
        }

    async def get(self, principal: Principal, thread_id: str) -> dict[str, Any]:
        issue = await self._get_issue(thread_id)
        self._authorize(issue, principal)
        comments = await self._scm.list_issue_comments(self._repo, int(issue["number"]), limit=200)
        thread = self._thread(issue, principal, include_replies=True)
        thread["replies"] = [self._reply(comment) for comment in comments]
        return thread

    async def reply(self, principal: Principal, thread_id: str, message: str) -> dict[str, Any]:
        message = _REPLY_MARKER_RE.sub("", message or "").strip()
        if not message or len(message) > 10000:
            raise HTTPException(status_code=422, detail="message is required and must be at most 10000 characters")
        issue = await self._get_issue(thread_id)
        self._authorize(issue, principal)
        if issue.get("state") != "open":
            raise HTTPException(status_code=409, detail="feedback thread is closed")

        author_role = "support" if self.can_manage(principal) else "user"
        author = principal.display_name or principal.email or principal.uid
        marker = _encoded_marker(
            "devai-feedback-reply",
            {"author": author, "author_role": author_role, "v": 1},
        )
        comment = await self._scm.add_comment(self._repo, int(issue["number"]), f"{message}\n\n{marker}")
        return self._reply(comment)

    async def set_status(self, principal: Principal, thread_id: str, status: str) -> dict[str, Any]:
        if not self.can_manage(principal):
            raise HTTPException(status_code=403, detail="support access required")
        if status not in {"open", "closed"}:
            raise HTTPException(status_code=422, detail="status must be open or closed")
        issue = await self._get_issue(thread_id)
        updated = await self._scm.update_issue(self._repo, int(issue["number"]), state=status)
        return self._thread(updated, principal, include_replies=False)

    async def _get_issue(self, thread_id: str) -> dict[str, Any]:
        try:
            number = int(thread_id)
            if number <= 0:
                raise ValueError
            issue = await self._scm.get_issue(self._repo, number)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=404, detail="feedback thread not found") from exc
        if "feedback" not in _labels(issue):
            raise HTTPException(status_code=404, detail="feedback thread not found")
        return issue

    def _thread(self, issue: dict[str, Any], principal: Principal, *, include_replies: bool) -> dict[str, Any]:
        metadata = _decode_last_marker(_ISSUE_MARKER_RE, str(issue.get("body") or "")) or {}
        state = "closed" if issue.get("state") == "closed" else "open"
        result = {
            "id": str(issue.get("number") or ""),
            "type": _kind(issue),
            "title": _title(issue),
            "description": _description(str(issue.get("body") or "")),
            "status": state,
            "issue_number": issue.get("number"),
            "issue_url": issue.get("html_url") or "",
            "submitter": metadata.get("submitter") or _legacy_submitter(issue),
            "created_at": issue.get("created_at") or "",
            "updated_at": issue.get("updated_at") or issue.get("created_at") or "",
            "can_reply": state == "open",
            "can_manage": self.can_manage(principal),
        }
        if include_replies:
            result["replies"] = []
        return result

    @staticmethod
    def _reply(comment: dict[str, Any]) -> dict[str, Any]:
        body = str(comment.get("body") or "")
        metadata = _decode_last_marker(_REPLY_MARKER_RE, body) or {}
        github_user = comment.get("user") or {}
        return {
            "id": str(comment.get("id") or ""),
            "body": _REPLY_MARKER_RE.sub("", body).rstrip(),
            "author": metadata.get("author") or github_user.get("login") or "DevAI support",
            "author_role": metadata.get("author_role") or "support",
            "created_at": comment.get("created_at") or "",
            "url": comment.get("html_url") or "",
        }
