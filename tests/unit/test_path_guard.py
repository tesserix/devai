"""Tests for workspace confinement of agent file-tool paths."""

import pytest

from devai.tools.path_guard import PathTraversalError, confine


def test_no_root_is_noop():
    # Empty root → confinement disabled, path returned unchanged.
    assert str(confine("/etc/passwd", "")) == "/etc/passwd"
    assert str(confine("../x", "")) == "../x"


def test_relative_path_resolved_under_root(tmp_path):
    (tmp_path / "doc.md").write_text("hi")
    out = confine("doc.md", str(tmp_path))
    assert out == (tmp_path / "doc.md").resolve()


def test_dotdot_traversal_blocked(tmp_path):
    with pytest.raises(PathTraversalError):
        confine("../../etc/passwd", str(tmp_path))


def test_absolute_escape_blocked(tmp_path):
    with pytest.raises(PathTraversalError):
        confine("/etc/passwd", str(tmp_path))


def test_absolute_inside_root_allowed(tmp_path):
    inside = tmp_path / "sub" / "f.txt"
    out = confine(str(inside), str(tmp_path))
    assert out == inside.resolve()
