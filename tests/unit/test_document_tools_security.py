"""Integration tests for the security guards wired into document tools:
workspace confinement (path traversal) and the SSRF guard on doc_read_url.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from devai.config import settings
from devai.tools.document_tools import DocumentToolExecutor


@pytest.fixture
def exec_() -> DocumentToolExecutor:
    return DocumentToolExecutor()


# ── Workspace confinement (path traversal) ────────────────────────────


async def test_markdown_read_blocked_outside_workspace(exec_, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tool_workspace_root", str(tmp_path))
    out = await exec_._handle_doc_read_markdown({"file_path": "../../../../etc/passwd"})
    assert "error" in out
    assert "escapes workspace root" in out["error"]


async def test_markdown_read_allowed_inside_workspace(exec_, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "tool_workspace_root", str(tmp_path))
    (tmp_path / "doc.md").write_text("# Title\n\nbody text\n")
    out = await exec_._handle_doc_read_markdown({"file_path": "doc.md"})
    assert "error" not in out
    assert out["total_sections"] >= 1
    assert "Title" in out["full_text"]


async def test_no_workspace_root_keeps_old_behavior(exec_, tmp_path, monkeypatch):
    # Empty root → no confinement; an absolute path inside tmp is readable.
    monkeypatch.setattr(settings, "tool_workspace_root", "")
    f = tmp_path / "x.md"
    f.write_text("# H\n")
    out = await exec_._handle_doc_read_markdown({"file_path": str(f)})
    assert "error" not in out


# ── SSRF guard on doc_read_url ────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "http://10.0.0.1/secret",
        "file:///etc/passwd",
    ],
)
async def test_doc_read_url_blocks_internal_targets(exec_, url):
    # assert_public_url rejects before any network call; the handler maps
    # the failure to an error dict.
    out = await exec_._handle_doc_read_url({"url": url})
    assert "error" in out


@respx.mock
async def test_doc_read_url_blocks_redirect_to_internal(exec_):
    # A public host (IP literal → no DNS) that 302s to a private host must
    # NOT be followed.
    respx.get("https://1.1.1.1/doc").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    out = await exec_._handle_doc_read_url({"url": "https://1.1.1.1/doc"})
    assert "error" in out


@respx.mock
async def test_doc_read_url_fetches_public_content(exec_):
    respx.get("https://8.8.8.8/page").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="hello world")
    )
    out = await exec_._handle_doc_read_url({"url": "https://8.8.8.8/page"})
    assert out.get("type") == "text"
    assert "hello world" in out["content"]
